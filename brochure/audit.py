"""
Helpers for the manual claim audit (the verdicts themselves are made by a person).

Sampling rule, fixed before looking at the results:
  In each improved brochure, take the FIRST TWO factual claims in reading order.
  A factual claim = a sentence (or bullet) in the body that carries at least one citation
  such as [A2] and is not a "not found in the sources" statement. Headings, the source list
  and table rows are not claims.
If a brochure has fewer than two such claims, that is recorded as a gap in the output.

For each sampled claim the helper shows lines from the cited sources that share words with
the claim (names, numbers, English terms). This only helps to find the evidence; whether the
source really supports the claim is decided by reading it.
"""

import csv
import json
import re
from pathlib import Path

from .checks import CITATION_RE, cited_ids, split_source_list
from .config import MISSING_INFO_PHRASE

AUDIT_COLUMNS = ["claim_no", "brochure", "run_id", "claim", "source_id", "url",
                 "evidence", "verdict", "note"]
VERDICTS = ["supported", "partly supported", "not supported", "contradicts the source"]


def _clean(line: str) -> str:
    line = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line)     # bullets / numbering
    return line.replace("**", "").replace("__", "").strip()


def factual_claims(text: str) -> list[str]:
    """All claims in reading order, following the sampling rule above."""
    body, _ = split_source_list(text)
    claims = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("|") or line.startswith(">"):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+(?=\S)", _clean(line)):
            sentence = sentence.strip()
            if CITATION_RE.search(sentence) and MISSING_INFO_PHRASE not in sentence:
                claims.append(sentence)
    return claims


def _words(text: str) -> set[str]:
    """Words of 3+ characters, plus any number (numbers are strong evidence across languages)."""
    text = CITATION_RE.sub(" ", text)
    words = set()
    for w in re.findall(r"[\w֐-׿][\w֐-׿']*", text):
        if len(w) >= 3 or any(ch.isdigit() for ch in w):
            words.add(w.lower())
    return words


def evidence_candidates(claim: str, source_text: str, top: int = 3) -> list[str]:
    """Lines of the source that share the most words with the claim (a reading aid only)."""
    claim_words = _words(claim)
    scored, seen = [], set()
    for line in source_text.splitlines():
        line = line.strip()
        if len(line) < 3 or line in seen:
            continue
        seen.add(line)
        overlap = len(claim_words & _words(line))
        if overlap:
            scored.append((overlap, line))
    scored.sort(key=lambda x: -x[0])
    return [line for _, line in scored[:top]]


def sample_claims(run_dir: Path, names: list[str], per_brochure: int = 2) -> list[dict]:
    """
    Apply the sampling rule to saved brochures of one run (no model calls).
    Returns one row per (claim, cited source) with the source URL and candidate evidence lines.
    """
    run_dir = Path(run_dir)
    sources = {}
    for path in (run_dir / "sources").glob("*.json"):
        for record in json.loads(path.read_text(encoding="utf-8"))["sources"]:
            sources[record["source_id"]] = record

    rows, claim_no = [], 0
    for name in names:
        text = (run_dir / "outputs" / f"{name}.md").read_text(encoding="utf-8")
        meta = json.loads((run_dir / "outputs" / f"{name}.json").read_text(encoding="utf-8"))
        claims = factual_claims(text)[:per_brochure]
        for missing in range(per_brochure - len(claims)):
            rows.append({"claim_no": None, "brochure": name, "run_id": meta["run_id"],
                         "claim": "(gap: the brochure has fewer than two factual claims)",
                         "source_id": "", "url": "", "candidates": []})
        for claim in claims:
            claim_no += 1
            for sid in cited_ids(claim):
                source = sources.get(sid, {})
                rows.append({
                    "claim_no": claim_no, "brochure": name, "run_id": meta["run_id"], "claim": claim,
                    "source_id": sid, "url": source.get("url", "(not in the saved sources)"),
                    "candidates": evidence_candidates(claim, source.get("text", "")),
                })
    return rows


def format_claims(rows: list[dict]) -> str:
    out = []
    for r in rows:
        out.append(f"#{r['claim_no']}  [{r['brochure']}]  {r['source_id']}  {r['url']}")
        out.append(f"   claim: {r['claim']}")
        for line in r["candidates"]:
            out.append(f"   source: {line[:220]}")
        out.append("")
    return "\n".join(out)


def write_audit_template(rows: list[dict], path: Path) -> Path:
    """CSV to fill in by hand (evidence quote, verdict, note). Will not overwrite a filled file."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"{path} already exists; not overwriting a filled-in audit.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:          # utf-8-sig: Hebrew opens in Excel
        writer = csv.DictWriter(f, fieldnames=AUDIT_COLUMNS)
        writer.writeheader()
        for r in rows:
            writer.writerow({"claim_no": r["claim_no"], "brochure": r["brochure"], "run_id": r["run_id"],
                             "claim": r["claim"], "source_id": r["source_id"], "url": r["url"],
                             "evidence": "", "verdict": "", "note": ""})
    return path
