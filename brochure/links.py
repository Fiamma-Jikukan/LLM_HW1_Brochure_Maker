"""
Candidate links: everything the code does to the home page's links BEFORE the model sees them.

Rules (applied in this order to every href on the home page):
  1. Empty or whitespace-only hrefs are dropped.
  2. mailto:, tel:, javascript: and any other non-http(s) scheme are dropped.
  3. Relative links are resolved against the home page's final URL (after redirects).
  4. The #fragment is removed.
  5. Only links on the configured site are kept. "The site" is the configured host,
     plus the host the home page redirected to, if different.
     www handling: "www.example.com" and "example.com" are treated as the same site.
     Other subdomains (e.g. jobs.example.com) and external domains are NOT the site.
  6. The URL is written in one canonical form: the home page's scheme and host
     (so www/no-www and http/https variants collapse into one), lowercase host,
     no default port, no trailing slash (except the root), query string kept.
  7. The home page itself (path "/", with any query string), privacy / terms / cookie / legal pages and links to files
     (pdf, images, archives...) are dropped - none of them is a valid extra page.
  8. Duplicates (after canonicalization) are dropped, keeping the first occurrence.

Every dropped href is kept with its reason, so the saved list documents the filtering.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from .config import MAX_EXTRA_PAGES, MAX_TOKENS, MODEL, Company, get_client
from .prompts import link_selection_messages

ALLOWED_SCHEMES = {"http", "https"}

# Privacy / terms detection, on the lowercased, URL-decoded path (English and Hebrew slugs).
# Unambiguous words match anywhere in a path segment ("privacy-policy", "privacypolicy").
FORBIDDEN_SUBSTRINGS = ["privacy", "cookie", "gdpr", "disclaimer", "פרטיות", "תקנון", "עוגיות"]
# Short or ambiguous words match only as whole words of a segment, so "/photos" is not "tos".
FORBIDDEN_WORDS = {"terms", "tos", "legal", "eula"}
# Hebrew "terms of use" (תנאי שימוש): both words in the same segment.
FORBIDDEN_WORD_PAIRS = [("תנאי", "שימוש")]
WORD_SEPARATORS = "-_. +"

FILE_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
    ".zip", ".rar", ".gz", ".mp4", ".mp3", ".mov", ".avi",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".xml", ".json",
)


def bare_host(host: str) -> str:
    """Lowercase host without a leading 'www.' - the unit used to decide 'same site'."""
    host = (host or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def site_hosts(company: Company, home_final_url: str | None) -> set[str]:
    """The bare hosts that count as the company's site."""
    hosts = {bare_host(urlsplit(company.url).hostname or "")}
    if home_final_url:
        hosts.add(bare_host(urlsplit(home_final_url).hostname or ""))
    hosts.discard("")
    return hosts


def _words(segment: str) -> set[str]:
    for sep in WORD_SEPARATORS:
        segment = segment.replace(sep, " ")
    return set(segment.split())


def _is_forbidden_path(path: str) -> bool:
    """True for privacy / terms / cookie / legal pages."""
    decoded = unquote(path).lower()
    for segment in (s for s in decoded.split("/") if s):
        if any(sub in segment for sub in FORBIDDEN_SUBSTRINGS):
            return True
        words = _words(segment)
        if words & FORBIDDEN_WORDS:
            return True
        if any(a in words and b in words for a, b in FORBIDDEN_WORD_PAIRS):
            return True
    return False


def normalize_url(raw: str | None, base_url: str, hosts: set[str]) -> tuple[str | None, str | None]:
    """
    Apply the rules above to one href.
    Returns (canonical_url, None) if it is a valid candidate, or (None, reason) if not.
    `base_url` is the home page's final URL; its scheme and host become the canonical form.
    """
    if raw is None or not raw.strip():
        return None, "empty"
    raw = raw.strip()

    scheme = urlsplit(raw).scheme.lower()
    if scheme and scheme not in ALLOWED_SCHEMES:
        return None, f"scheme '{scheme}' not allowed"

    absolute = urljoin(base_url, raw)
    parts = urlsplit(absolute)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return None, f"scheme '{parts.scheme}' not allowed"

    host = parts.hostname or ""
    if bare_host(host) not in hosts:
        return None, f"outside the site ({host or 'no host'})"

    base = urlsplit(base_url)
    path = parts.path or "/"
    while "//" in path:
        path = path.replace("//", "/")
    if len(path) > 1:
        path = path.rstrip("/")

    canonical = urlunsplit((base.scheme.lower(), (base.hostname or "").lower(), path, parts.query, ""))

    if path == "/":
        return None, "home page"   # with or without a query string (e.g. /?lang=he)
    if _is_forbidden_path(path):
        return None, "privacy / terms / legal page"
    if path.lower().endswith(FILE_EXTENSIONS):
        return None, "file, not a page"
    return canonical, None


def canonical_home(home_final_url: str) -> str:
    """The home page in canonical form (used to reject it if the model selects it)."""
    parts = urlsplit(home_final_url)
    return urlunsplit((parts.scheme.lower(), (parts.hostname or "").lower(), "/", "", ""))


def build_candidates(company: Company, home_page: dict) -> dict:
    """
    Turn the home page result of scraper.fetch_page() into the candidate list sent to the model.
    Returns a dict that is saved as-is, so the exact list given to the model is on record.
    """
    base_url = home_page.get("final_url") or company.url
    hosts = site_hosts(company, home_page.get("final_url"))

    candidates: list[str] = []
    dropped: list[dict] = []
    seen: set[str] = set()

    for raw in home_page.get("links", []):
        url, reason = normalize_url(raw, base_url, hosts)
        if url is None:
            dropped.append({"href": raw, "reason": reason})
        elif url in seen:
            dropped.append({"href": raw, "reason": f"duplicate of {url}"})
        else:
            seen.add(url)
            candidates.append(url)

    return {
        "company_id": company.company_id,
        "configured_url": company.url,
        "home_final_url": home_page.get("final_url"),
        "site_hosts": sorted(hosts),
        "raw_link_count": len(home_page.get("links", [])),
        "candidates": candidates,
        "dropped": dropped,
    }


def save_candidates(candidates: dict, directory: Path) -> Path:
    """Write the candidate list to <directory>/<company_id>.json and return the path."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{candidates['company_id']}.json"
    path.write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ===========================================================================
# Link selection by the model, and validation of its answer
# ===========================================================================
#
# The model must return {"links": [{"type": str, "url": str, "reason": str}, ...]}.
# The request asks for this shape with a strict JSON schema, but the answer is still
# validated in code before ANY page is fetched. If anything is wrong, the whole answer is
# rejected (nothing is silently dropped or repaired) and no page from it is fetched.

LINK_ITEM_FIELDS = ("type", "url", "reason")

LINK_SELECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "links": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {field: {"type": "string"} for field in LINK_ITEM_FIELDS},
                "required": list(LINK_ITEM_FIELDS),
                "additionalProperties": False,
            },
        }
    },
    "required": ["links"],
    "additionalProperties": False,
}

# Anthropic's OpenAI-compatible endpoint accepts only json_schema with strict=True
# (json_object, as used in the class notebook, returns a 400 error there).
LINK_SELECTION_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "link_selection", "strict": True, "schema": LINK_SELECTION_SCHEMA},
}


def validate_selection(raw_output: str | None, candidates: dict) -> dict:
    """
    Check the model's raw answer against the contract and the candidate list.

    `candidates` is the dict returned by build_candidates().
    Returns {"valid": bool, "links": [...], "errors": [...]}.
    `links` is filled only when valid; each item keeps the model's fields and adds
    "normalized_url", the canonical URL that will be fetched.
    Every problem found is listed in `errors` (validation does not stop at the first one).
    """
    errors: list[str] = []

    if raw_output is None or not raw_output.strip():
        return {"valid": False, "links": [], "errors": ["empty response"]}
    try:
        data = json.loads(raw_output)
    except json.JSONDecodeError as e:
        return {"valid": False, "links": [], "errors": [f"invalid JSON: {e}"]}

    if not isinstance(data, dict):
        return {"valid": False, "links": [], "errors": ["top level is not a JSON object"]}
    if "links" not in data:
        return {"valid": False, "links": [], "errors": ["missing field 'links'"]}
    extra_keys = set(data) - {"links"}
    if extra_keys:
        errors.append(f"unexpected top-level fields: {sorted(extra_keys)}")
    items = data["links"]
    if not isinstance(items, list):
        return {"valid": False, "links": [], "errors": errors + ["'links' is not a list"]}
    if len(items) > MAX_EXTRA_PAGES:
        errors.append(f"{len(items)} links selected; at most {MAX_EXTRA_PAGES} are allowed")

    base_url = candidates.get("home_final_url") or candidates["configured_url"]
    hosts = set(candidates["site_hosts"])
    allowed = set(candidates["candidates"])
    seen: dict[str, int] = {}
    accepted: list[dict] = []

    for i, item in enumerate(items, start=1):
        where = f"item {i}"
        if not isinstance(item, dict):
            errors.append(f"{where}: not an object")
            continue
        missing = [f for f in LINK_ITEM_FIELDS if f not in item]
        if missing:
            errors.append(f"{where}: missing fields {missing}")
        extra = set(item) - set(LINK_ITEM_FIELDS)
        if extra:
            errors.append(f"{where}: unexpected fields {sorted(extra)}")
        for field in LINK_ITEM_FIELDS:
            if field in item and (not isinstance(item[field], str) or not item[field].strip()):
                errors.append(f"{where}: '{field}' must be a non-empty string")

        url = item.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        normalized, reason = normalize_url(url, base_url, hosts)
        if normalized is None:
            errors.append(f"{where}: {url!r} breaks the site/filter rules ({reason})")
            continue
        if normalized not in allowed:
            errors.append(f"{where}: {url!r} is not in the candidate list sent to the model")
        if normalized in seen:
            errors.append(f"{where}: {url!r} duplicates item {seen[normalized]} after normalization")
        else:
            seen[normalized] = i
        accepted.append({**item, "normalized_url": normalized})

    if errors:
        return {"valid": False, "links": [], "errors": errors}
    return {"valid": True, "links": accepted, "errors": []}


def call_link_selection(messages: list[dict]) -> dict:
    """
    One model call (no retries). Returns the raw text and call metadata;
    a failed call is returned as data (error set, raw_output None), not raised.
    """
    started = time.perf_counter()
    result = {
        "model": MODEL,
        "raw_output": None,
        "finish_reason": None,
        "usage": None,
        "error": None,
        "duration_seconds": None,
    }
    try:
        response = get_client().chat.completions.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=messages,
            response_format=LINK_SELECTION_RESPONSE_FORMAT,
        )
        choice = response.choices[0] if response.choices else None
        result["raw_output"] = choice.message.content if choice else None
        result["finish_reason"] = choice.finish_reason if choice else None
        if response.usage:
            result["usage"] = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
    except Exception as e:  # network errors, API errors (4xx/5xx), timeouts
        result["error"] = f"{type(e).__name__}: {e}"
    result["duration_seconds"] = round(time.perf_counter() - started, 2)
    return result


def select_links(company: Company, candidates: dict, call=call_link_selection) -> dict:
    """
    Build the prompt, call the model, validate the answer.
    Returns a selection record (saved as-is) with:
      status: "accepted" | "rejected" | "call_failed"
      links:  the validated links to fetch, in the model's order (empty unless accepted)
    `call` can be replaced by a fake in tests, so no real model call is made there.
    """
    messages = link_selection_messages(company, candidates["candidates"])
    call_result = call(messages)

    if call_result.get("error"):
        validation = {"valid": False, "links": [], "errors": [f"model call failed: {call_result['error']}"]}
        status = "call_failed"
    else:
        validation = validate_selection(call_result.get("raw_output"), candidates)
        if call_result.get("finish_reason") == "length" and validation["valid"]:
            validation = {"valid": False, "links": [], "errors": ["response cut off (finish_reason=length)"]}
        status = "accepted" if validation["valid"] else "rejected"

    return {
        "company_id": company.company_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "links": validation["links"],
        "rejection_reasons": validation["errors"],
        "messages": messages,
        **call_result,
    }


def links_to_fetch(selection: dict) -> list[dict]:
    """
    The only way to get URLs to fetch from a selection: refuses unless it was accepted.
    This is the guard that stops fetching when the model's answer was rejected.
    """
    if selection.get("status") != "accepted":
        raise ValueError(
            f"Link selection for '{selection.get('company_id')}' was {selection.get('status')}; "
            f"no pages are fetched from it. Reasons: {selection.get('rejection_reasons')}"
        )
    return selection["links"]


def save_selection(selection: dict, directory: Path) -> Path:
    """Write the selection record (prompt, raw output, status, reasons) to <directory>/<company_id>.json."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{selection['company_id']}.json"
    path.write_text(json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
