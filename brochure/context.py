"""
Context builder: turns saved source collections into the source block of a prompt,
within an explicit CHARACTER budget for source text (config.CONTEXT_CHAR_BUDGET).

Policy
  - Only sources with status "ok" are used. Empty and failed sources are left out
    and listed in the report with the reason.
  - The budget covers source TEXT only. Instructions (added by the prompt builders),
    company headers, source tags with their IDs, URLs and titles, and truncation markers
    are outside the budget and are never cut.
  - Fair split ("water-filling"): every source gets an equal share of the budget; a source
    shorter than its share is included whole and the unused part is shared among the rest.
    So a long home page cannot push the other sources out - it is shortened instead.
  - With several companies (the comparison call), all companies share ONE budget:
    it is first split fairly between companies, then between each company's sources.
  - A shortened text is cut at a line break (or a space) near its limit and ends with a
    visible marker saying how much was kept.
  - The report lists every source as included / shortened / left out, with the reason.
"""

import json
from pathlib import Path

from .config import CONTEXT_CHAR_BUDGET


def fair_shares(lengths: list[int], budget: int) -> list[int]:
    """
    Split `budget` between items whose full sizes are `lengths`.
    Items that fit within an equal share get their full size; the rest share what is left equally.
    """
    shares = [0] * len(lengths)
    remaining_budget = budget
    remaining = sorted(range(len(lengths)), key=lambda i: lengths[i])
    while remaining:
        share = remaining_budget // len(remaining)
        i = remaining[0]
        if lengths[i] <= share:
            shares[i] = lengths[i]
            remaining_budget -= lengths[i]
            remaining.pop(0)
        else:
            # every remaining item is at least this long: give each an equal share
            for j in remaining:
                shares[j] = share
            remaining_budget -= share * len(remaining)
            # hand out the leftover characters from the integer division, one each
            for j in remaining[:remaining_budget]:
                shares[j] += 1
            break
    return shares


def cut_text(text: str, limit: int) -> str:
    """Cut to at most `limit` characters, preferring a line break, then a space, near the end."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("\n", " "):
        pos = cut.rfind(sep)
        if pos >= int(limit * 0.8):
            return cut[:pos].rstrip()
    return cut


def _source_block(source: dict, text: str, shortened: bool) -> str:
    title = (source["title"] or "").replace('"', "'").replace("\n", " ")
    header = f'<source id="{source["source_id"]}" url="{source["url"]}" title="{title}">'
    body = text
    if shortened:
        body += f"\n[... shortened: kept {len(text):,} of {len(source['text']):,} characters]"
    return f"{header}\n{body}\n</source>"


def build_context(collections: list[dict], budget: int = CONTEXT_CHAR_BUDGET) -> dict:
    """
    Build the source block for one model call.
    `collections` holds one saved collection (a brochure) or two (the comparison).
    Returns {"text", "budget", "source_chars_used", "included_ids", "report"}.
    """
    usable_per_company = [[s for s in c["sources"] if s["status"] == "ok"] for c in collections]
    company_totals = [sum(len(s["text"]) for s in usable) for usable in usable_per_company]
    company_budgets = fair_shares(company_totals, budget)

    report: list[dict] = []
    blocks: list[str] = []
    included_ids: list[str] = []
    used = 0

    for collection, usable, company_budget in zip(collections, usable_per_company, company_budgets):
        shares = fair_shares([len(s["text"]) for s in usable], company_budget)
        share_by_id = {s["source_id"]: share for s, share in zip(usable, shares)}

        company_blocks = []
        for source in collection["sources"]:
            sid = source["source_id"]
            full = len(source["text"])
            if source["status"] != "ok":
                report.append({
                    "source_id": sid, "url": source["url"], "decision": "left out",
                    "full_chars": full, "used_chars": 0,
                    "reason": f"status '{source['status']}' - not usable as evidence ({source.get('error')})",
                })
                continue
            text = cut_text(source["text"], share_by_id[sid])
            shortened = len(text) < full
            if shortened:
                reason = f"shortened to its fair share of the budget ({share_by_id[sid]:,} characters)"
            else:
                reason = "fits within its share of the budget"
            report.append({
                "source_id": sid, "url": source["url"],
                "decision": "shortened" if shortened else "included",
                "full_chars": full, "used_chars": len(text), "reason": reason,
            })
            company_blocks.append(_source_block(source, text, shortened))
            included_ids.append(sid)
            used += len(text)

        heading = f"## Company {collection['letter']}: {collection['company_name']} ({collection['home_url']})"
        if not company_blocks:
            company_blocks = ["(no usable sources for this company)"]
        blocks.append(heading + "\n\n" + "\n\n".join(company_blocks))

    return {
        "text": "\n\n".join(blocks),
        "budget": budget,
        "source_chars_used": used,
        "included_ids": included_ids,
        "report": report,
    }


def format_report(context: dict) -> str:
    """Human-readable version of the inclusion report."""
    lines = [f"Source text used: {context['source_chars_used']:,} of {context['budget']:,} characters"]
    for r in context["report"]:
        lines.append(
            f"  {r['source_id']:<3} {r['decision']:<10} {r['used_chars']:>6,} / {r['full_chars']:>6,}  {r['reason']}"
        )
    return "\n".join(lines)


def save_context(context: dict, path: Path) -> Path:
    """Save the exact context text and its report (JSON) next to each other."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_suffix(".txt").write_text(context["text"], encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    return path.with_suffix(".json")
