"""
Checks on generated outputs, done in code (no model calls).

For every output:
  - citations: every cited ID (e.g. [A2], [A1][A3], [A1, A3]) must be one of the IDs that were
    included in the context of THAT call; the body must contain at least one citation.
  - source list: a closing "## מקורות" section must exist; each line "- [A1] URL" must use an
    included ID with the same URL as in the context, and every ID cited in the body must be listed.
  - no code blocks and no literal "None" in the text.
For brochures also:
  - word count with str.split() on the text BEFORE the source list (headings included),
    which must be within MIN_WORDS..MAX_WORDS;
  - for the improved variant, the required section headings, in order.
For the comparison also:
  - the table must contain all 4 comparison dimensions.

Note: these checks confirm that a cited source EXISTS in the context. They do not show that
the source actually SUPPORTS the claim - that is what the manual claim audit is for.
"""

import json
import re
from pathlib import Path

from .config import (
    COMPARISON_DIMENSIONS,
    MAX_WORDS,
    MIN_WORDS,
    MISSING_INFO_PHRASE,
    SOURCES_HEADING,
)
from .prompts import AUDIENCE_GUIDES

CITATION_RE = re.compile(r"\[\s*([A-Z]\d+(?:\s*[,;،]\s*[A-Z]\d+)*)\s*\]")
SOURCE_LINE_RE = re.compile(r"^\s*[-*]?\s*\[\s*([A-Z]\d+)\s*\]\s*:?\s*(\S+)")


def split_source_list(text: str) -> tuple[str, str | None]:
    """Return (body, source_list). The source list starts at the last line equal to SOURCES_HEADING."""
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == SOURCES_HEADING:
            return "\n".join(lines[:i]), "\n".join(lines[i + 1:])
    return text, None


def count_words(text: str) -> int:
    """Word count used for the length requirement: str.split() on the text before the source list."""
    body, _ = split_source_list(text)
    return len(body.split())


def cited_ids(text: str) -> list[str]:
    ids: set[str] = set()
    for group in CITATION_RE.findall(text):
        ids.update(part.strip() for part in re.split(r"[,;،]", group))
    return sorted(ids)


def _norm_url(url: str) -> str:
    return url.strip().rstrip("/").rstrip(".,;)").rstrip("/").lower()


def _included_urls(context: dict) -> dict[str, str]:
    return {r["source_id"]: r["url"] for r in context["report"] if r["decision"] != "left out"}


def expected_headings(company_name: str, audience: str) -> list[str]:
    """The heading lines the improved prompt asks for, for this company and audience."""
    guide = AUDIENCE_GUIDES[audience].replace("{name}", company_name)
    return [line.strip() for line in guide.splitlines() if line.startswith("#")]


def _norm_heading(line: str) -> str:
    return " ".join(line.strip().split())


def check_output(text: str, context: dict, kind: str, company_name: str | None = None,
                 audience: str | None = None, variant: str = "improved") -> dict:
    """
    Run all checks. kind: "brochure" or "comparison".
    Returns a dict with the details and "passed" / "problems" (hard failures).
    """
    problems: list[str] = []
    included = list(context["included_ids"])
    urls = _included_urls(context)
    body, source_list = split_source_list(text)

    # Citations in the body
    body_ids = cited_ids(body)
    unknown = [i for i in body_ids if i not in included]
    if unknown:
        problems.append(f"cited IDs not in this call's context: {unknown}")
    if not body_ids:
        problems.append("no citations in the text")

    # Source list
    listed: list[dict] = []
    if source_list is None:
        problems.append(f"no source list (missing '{SOURCES_HEADING}')")
    else:
        for line in source_list.splitlines():
            m = SOURCE_LINE_RE.match(line)
            if m:
                listed.append({"source_id": m.group(1), "url": m.group(2)})
        listed_ids = {item["source_id"] for item in listed}
        for item in listed:
            sid = item["source_id"]
            if sid not in included:
                problems.append(f"source list has {sid}, which is not in this call's context")
            elif _norm_url(item["url"]) != _norm_url(urls[sid]):
                problems.append(f"source list URL for {sid} is {item['url']}, context has {urls[sid]}")
        not_listed = [i for i in body_ids if i in included and i not in listed_ids]
        if not_listed:
            problems.append(f"cited but missing from the source list: {not_listed}")

    # Format
    if "```" in text:
        problems.append("contains a code block")
    if re.search(r"\bNone\b", text):
        problems.append("contains the literal word 'None'")

    result = {
        "kind": kind,
        "cited_ids": body_ids,
        "included_ids": included,
        "unknown_ids": unknown,
        "source_list": listed,
        "missing_info_mentions": text.count(MISSING_INFO_PHRASE),
    }

    if kind == "brochure":
        words = len(body.split())
        result["word_count"] = words
        result["word_range"] = [MIN_WORDS, MAX_WORDS]
        result["word_count_ok"] = MIN_WORDS <= words <= MAX_WORDS
        if not result["word_count_ok"]:
            problems.append(f"word count {words} is outside {MIN_WORDS}-{MAX_WORDS}")
        if variant == "improved" and company_name and audience:
            expected = expected_headings(company_name, audience)
            found = [_norm_heading(line) for line in body.splitlines() if line.lstrip().startswith("#")]
            missing = [h for h in expected if _norm_heading(h) not in found]
            positions = [found.index(_norm_heading(h)) for h in expected if _norm_heading(h) in found]
            in_order = positions == sorted(positions)
            result["structure"] = {"expected": expected, "missing": missing, "in_order": in_order}
            if missing:
                problems.append(f"missing required headings: {missing}")
            elif not in_order:
                problems.append("required headings are not in the required order")

    elif kind == "comparison":
        table_lines = [line for line in body.splitlines() if line.strip().startswith("|")]
        missing_dims = [label for _, label in COMPARISON_DIMENSIONS
                        if not any(label in line for line in table_lines)]
        result["table_rows"] = len(table_lines)
        result["missing_dimensions"] = missing_dims
        if not table_lines:
            problems.append("no Markdown table")
        elif missing_dims:
            problems.append(f"table is missing dimensions: {missing_dims}")

    result["problems"] = problems
    result["passed"] = not problems
    return result


def format_checks(checks: dict) -> str:
    """One short human-readable summary."""
    parts = [("PASSED" if checks["passed"] else "FAILED")]
    if "word_count" in checks:
        parts.append(f"words={checks['word_count']} (range {checks['word_range'][0]}-{checks['word_range'][1]})")
    parts.append(f"cited={checks['cited_ids']} of included={checks['included_ids']}")
    parts.append(f"missing-info phrase x{checks['missing_info_mentions']}")
    lines = [" | ".join(parts)]
    lines += [f"  - {p}" for p in checks["problems"]]
    return "\n".join(lines)


def check_saved_output(run_dir: Path, name: str) -> dict:
    """
    Re-run the checks from saved files only (no model call): outputs/<name>.md,
    outputs/<name>.json (metadata) and contexts/<name>.json. Used to reproduce the results.
    """
    run_dir = Path(run_dir)
    text = (run_dir / "outputs" / f"{name}.md").read_text(encoding="utf-8")
    meta = json.loads((run_dir / "outputs" / f"{name}.json").read_text(encoding="utf-8"))
    context = json.loads((run_dir / "contexts" / f"{name}.json").read_text(encoding="utf-8"))
    return check_output(
        text, context, kind=meta["action"], company_name=meta.get("company_name"),
        audience=meta.get("audience"), variant=meta.get("variant", "improved"),
    )
