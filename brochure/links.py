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
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from .config import Company

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
