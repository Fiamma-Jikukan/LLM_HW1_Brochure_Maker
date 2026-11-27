"""
Source collection: fetch the home page and the validated extra pages once, give each an ID,
and save the result. Every later step (all brochures, the comparison, the baseline) reads
this saved collection - the site is never selected or fetched again per audience.

Record fields:
  source_id     A1..A4 for the first company, B1..B4 for the second.
                The home page is always X1; extra pages follow the order the model chose.
  company_id, url (the canonical URL requested), final_url (after redirects), title,
  retrieved_at  (UTC, ISO 8601), text (full page text, not truncated), status,
  http_status, error, note (why the page was added, e.g. the model's reason)

Status:
  ok      fetched and has at least MIN_TEXT_CHARS characters of text - usable as evidence
  empty   fetched but (almost) no text, e.g. a page built only by JavaScript - not evidence
  failed  network error, HTTP error status or not an HTML page - not evidence
Empty and failed records are kept, so what is missing stays visible.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from scraper import fetch_page

from .config import (
    FETCH_DELAY_SECONDS,
    FETCH_TIMEOUT_SECONDS,
    MAX_EXTRA_PAGES,
    MIN_TEXT_CHARS,
    Company,
)
from .links import links_to_fetch, normalize_url

MAX_SOURCES = 1 + MAX_EXTRA_PAGES   # home page + extra pages -> IDs X1..X4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch_home(company: Company, fetch=fetch_page) -> dict:
    """Fetch the home page once. Its links feed the candidate list, its text becomes source X1."""
    page = fetch(company.url, timeout=FETCH_TIMEOUT_SECONDS)
    page["retrieved_at"] = _now()
    return page


def page_status(page: dict) -> str:
    if page.get("error"):
        return "failed"
    if len(page.get("text", "").strip()) < MIN_TEXT_CHARS:
        return "empty"
    return "ok"


def make_record(company: Company, number: int, url: str, page: dict, note: str) -> dict:
    status = page_status(page)
    error = page.get("error")
    if status == "empty" and not error:
        error = f"only {len(page.get('text', '').strip())} characters of text (minimum {MIN_TEXT_CHARS})"
    return {
        "source_id": f"{company.letter}{number}",
        "company_id": company.company_id,
        "url": url,
        "final_url": page.get("final_url"),
        "title": page.get("title", ""),
        "retrieved_at": page.get("retrieved_at") or _now(),
        "text": page.get("text", ""),
        "status": status,
        "http_status": page.get("http_status"),
        "error": error,
        "note": note,
    }


def collect_sources(company: Company, home_page: dict, selection: dict, fetch=fetch_page) -> dict:
    """
    Build the source collection for one company.
    `home_page` is the result of fetch_home() (reused, not fetched again).
    `selection` must be an accepted link selection: links_to_fetch() raises before any
    request is made if it was rejected or the call failed.
    `fetch` can be replaced by a fake in tests.
    """
    links = links_to_fetch(selection)   # guard: raises if the selection was not accepted

    home_url = home_page.get("final_url") or company.url
    records = [make_record(company, 1, home_url, home_page, "home page")]
    site_requests = 1                   # the home page request

    for number, link in enumerate(links, start=2):
        time.sleep(FETCH_DELAY_SECONDS)
        page = fetch(link["normalized_url"], timeout=FETCH_TIMEOUT_SECONDS)
        page["retrieved_at"] = _now()
        site_requests += 1
        note = f"selected by the model as '{link['type']}': {link['reason']}"
        records.append(make_record(company, number, link["normalized_url"], page, note))

    return {
        "company_id": company.company_id,
        "company_name": company.name,
        "letter": company.letter,
        "home_url": company.url,
        "site_requests": site_requests,
        "replacements": [],
        "sources": records,
    }


def add_replacement_source(
    collection: dict, candidates: dict, url: str, reason: str, fetch=fetch_page
) -> dict:
    """
    Add a replacement page when there is not enough usable information.
    The page limit still applies: a replacement can only take a free ID (X2..X4 not yet used),
    so failed or empty records stay in place and remain visible. The URL must pass the
    same site/filter rules and be in the saved candidate list. The reason is recorded.
    """
    records = collection["sources"]
    if len(records) >= MAX_SOURCES:
        raise ValueError(
            f"{collection['company_id']} already has {MAX_SOURCES} sources; no free ID for a replacement."
        )
    base_url = candidates.get("home_final_url") or candidates["configured_url"]
    normalized, rule_error = normalize_url(url, base_url, set(candidates["site_hosts"]))
    if normalized is None:
        raise ValueError(f"{url!r} breaks the site/filter rules ({rule_error}).")
    if normalized not in candidates["candidates"]:
        raise ValueError(f"{url!r} is not in the saved candidate list.")
    if any(r["url"] == normalized for r in records):
        raise ValueError(f"{url!r} is already in the collection.")

    company = Company(collection["company_id"], collection["company_name"], collection["home_url"], collection["letter"])
    time.sleep(FETCH_DELAY_SECONDS)
    page = fetch(normalized, timeout=FETCH_TIMEOUT_SECONDS)
    page["retrieved_at"] = _now()
    collection["site_requests"] += 1
    record = make_record(company, len(records) + 1, normalized, page, f"replacement: {reason}")
    records.append(record)
    collection["replacements"].append({"source_id": record["source_id"], "url": normalized, "reason": reason})
    return record


def usable_sources(collection: dict) -> list[dict]:
    """Only status 'ok' sources may be used as evidence."""
    return [r for r in collection["sources"] if r["status"] == "ok"]


def summarize_sources(collection: dict) -> str:
    """A short table of the collection, including what is missing and why."""
    lines = [f"{collection['company_name']} ({collection['site_requests']} requests to the site)"]
    for r in collection["sources"]:
        line = f"  {r['source_id']}  {r['status']:<6}  {len(r['text']):>6} chars  {r['url']}"
        if r["status"] != "ok":
            line += f"  -> not used as evidence ({r['error']})"
        lines.append(line)
    usable = len(usable_sources(collection))
    lines.append(f"  usable sources: {usable} of {len(collection['sources'])}")
    return "\n".join(lines)


def save_sources(collection: dict, directory: Path) -> Path:
    """Write the collection (with full texts) to <directory>/<company_id>.json."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{collection['company_id']}.json"
    path.write_text(json.dumps(collection, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_sources(path: Path) -> dict:
    """Read a saved collection; all generation steps work from this saved copy."""
    return json.loads(Path(path).read_text(encoding="utf-8"))
