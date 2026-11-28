"""
Context builder: turns saved source collections into the source block of a prompt,
within an explicit CHARACTER budget for source text (config.CONTEXT_CHAR_BUDGET).

Policy
  - Only sources with status "ok" are used. Empty and failed sources are left out
    and listed in the report with the reason.
  - Site-wide menu text (applied before the budget, when remove_site_wide=True):
    many sites repeat the same navigation menu at the top of every page, so the start of
    each page is mostly menu. A line that appears on EVERY usable page of a company is
    treated as site-wide text:
      * on the home page (X1) it is moved to the end of the page's text, after a separator,
        so the page's own text comes first and the menu can still be cited if room is left;
      * on the other pages (X2..X4) it is removed, since it is already available in X1.
    A page other than the home page that is ONLY site-wide text is left out (reason recorded).
    It is only applied when a company has at least two usable pages. The saved source text
    is never changed: this is done only when the context is built, the same way every time,
    and the report shows how many characters were moved or removed per source.
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

SITE_WIDE_SEPARATOR = "[Site-wide menu and navigation text, repeated on every page of this site:]"


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


def site_wide_lines(usable: list[dict]) -> set[str]:
    """Lines that appear on every usable page of one company (empty if it has fewer than 2)."""
    if len(usable) < 2:
        return set()
    shared = None
    for source in usable:
        lines = {line for line in source["text"].split("\n") if line.strip()}
        shared = lines if shared is None else shared & lines
    return shared or set()


def separate_site_wide(source: dict, shared: set[str], is_home: bool) -> tuple[str, int]:
    """
    Return (text to use, characters of site-wide text moved or removed).
    Home page: its own lines first, then the separator and the site-wide lines.
    Other pages: only their own lines.
    """
    if not shared:
        return source["text"], 0
    lines = source["text"].split("\n")
    own = [line for line in lines if line not in shared]
    menu = [line for line in lines if line in shared]
    if not menu:
        return source["text"], 0
    own_text = "\n".join(own)
    menu_chars = len(source["text"]) - len(own_text)
    if is_home:
        return own_text + "\n\n" + SITE_WIDE_SEPARATOR + "\n" + "\n".join(menu), menu_chars
    return own_text, menu_chars


def _source_block(source: dict, text: str, available: int, shortened: bool) -> str:
    title = (source["title"] or "").replace('"', "'").replace("\n", " ")
    header = f'<source id="{source["source_id"]}" url="{source["url"]}" title="{title}">'
    body = text
    if shortened:
        body += f"\n[... shortened: kept {len(text):,} of {available:,} characters]"
    return f"{header}\n{body}\n</source>"


def build_context(collections: list[dict], budget: int = CONTEXT_CHAR_BUDGET,
                  remove_site_wide: bool = True) -> dict:
    """
    Build the source block for one model call.
    `collections` holds one saved collection (a brochure) or two (the comparison).
    `remove_site_wide=False` reproduces contexts built before site-wide text handling existed.
    Returns {"text", "budget", "site_wide_removal", "source_chars_used", "included_ids", "report"}.
    """
    prepared = []   # per company: list of (source, text to use, site-wide chars) for usable sources
    for collection in collections:
        usable = [s for s in collection["sources"] if s["status"] == "ok"]
        shared = site_wide_lines(usable) if remove_site_wide else set()
        home_id = f"{collection['letter']}1"
        prepared.append([(s, *separate_site_wide(s, shared, s["source_id"] == home_id)) for s in usable])

    company_totals = [sum(len(text) for _, text, _ in items) for items in prepared]
    company_budgets = fair_shares(company_totals, budget)

    report: list[dict] = []
    blocks: list[str] = []
    included_ids: list[str] = []
    used = 0

    for collection, items, company_budget in zip(collections, prepared, company_budgets):
        shares = fair_shares([len(text) for _, text, _ in items], company_budget)
        by_id = {s["source_id"]: (text, menu_chars, share) for (s, text, menu_chars), share in zip(items, shares)}
        home_id = f"{collection['letter']}1"

        company_blocks = []
        for source in collection["sources"]:
            sid = source["source_id"]
            raw = len(source["text"])
            if source["status"] != "ok":
                report.append({
                    "source_id": sid, "url": source["url"], "decision": "left out",
                    "full_chars": raw, "site_wide_chars": 0, "available_chars": 0, "used_chars": 0,
                    "reason": f"status '{source['status']}' - not usable as evidence ({source.get('error')})",
                })
                continue

            available_text, menu_chars, share = by_id[sid]
            if not available_text.strip():
                report.append({
                    "source_id": sid, "url": source["url"], "decision": "left out",
                    "full_chars": raw, "site_wide_chars": menu_chars, "available_chars": 0, "used_chars": 0,
                    "reason": f"only site-wide menu text ({menu_chars:,} chars), which is already in {home_id}",
                })
                continue
            text = cut_text(available_text, share)
            shortened = len(text) < len(available_text)

            notes = []
            if menu_chars:
                action = "moved to the end" if sid == home_id else "removed"
                notes.append(f"site-wide menu text {action} ({menu_chars:,} chars)")
            if shortened:
                notes.append(f"shortened to its fair share of the budget ({share:,} characters)")
            else:
                notes.append("fits within its share of the budget")
            report.append({
                "source_id": sid, "url": source["url"],
                "decision": "shortened" if shortened else "included",
                "full_chars": raw, "site_wide_chars": menu_chars,
                "available_chars": len(available_text), "used_chars": len(text),
                "reason": "; ".join(notes),
            })
            company_blocks.append(_source_block(source, text, len(available_text), shortened))
            included_ids.append(sid)
            used += len(text)

        heading = f"## Company {collection['letter']}: {collection['company_name']} ({collection['home_url']})"
        if not company_blocks:
            company_blocks = ["(no usable sources for this company)"]
        blocks.append(heading + "\n\n" + "\n\n".join(company_blocks))

    return {
        "text": "\n\n".join(blocks),
        "budget": budget,
        "site_wide_removal": remove_site_wide,
        "source_chars_used": used,
        "included_ids": included_ids,
        "report": report,
    }


def rebuild_context(saved_context: dict, collections: list[dict]) -> dict:
    """
    Rebuild a context from saved sources with the SAME settings it was built with
    (budget and site-wide handling, as recorded in the saved context). Used to replay a run.
    Contexts saved before the setting existed were built without site-wide handling.
    """
    return build_context(collections, budget=saved_context["budget"],
                         remove_site_wide=saved_context.get("site_wide_removal", False))


def format_report(context: dict) -> str:
    """Human-readable version of the inclusion report."""
    lines = [f"Source text used: {context['source_chars_used']:,} of {context['budget']:,} characters"
             f" (site-wide menu handling: {'on' if context.get('site_wide_removal') else 'off'})"]
    for r in context["report"]:
        lines.append(
            f"  {r['source_id']:<3} {r['decision']:<10} {r['used_chars']:>6,} / {r['full_chars']:>6,}  {r['reason']}"
        )
    return "\n".join(lines)


def write_text_exact(path: Path, text: str) -> None:
    """Write text byte-for-byte: no newline conversion (on Windows, "\\n" would otherwise become "\\r\\n")."""
    Path(path).write_text(text, encoding="utf-8", newline="")


def read_text_exact(path: Path) -> str:
    """Read text byte-for-byte: no newline conversion ("\\r" and "\\r\\n" are kept as they are)."""
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def save_context(context: dict, path: Path) -> Path:
    """
    Save the exact context text (.txt) and the context with its report (.json) next to each other.
    Both are exact copies of the text sent: the .txt is written without newline conversion.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_exact(path.with_suffix(".txt"), context["text"])
    path.with_suffix(".json").write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    return path.with_suffix(".json")
