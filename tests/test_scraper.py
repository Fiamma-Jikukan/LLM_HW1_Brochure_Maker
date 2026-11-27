"""scraper.fetch_page with fake HTTP responses (requests.get is replaced; no network)."""

from types import SimpleNamespace

import requests

import scraper


def fake_response(url="https://acme.com/", status=200, content_type="text/html; charset=utf-8", html=""):
    return SimpleNamespace(url=url, status_code=status, ok=200 <= status < 400,
                           headers={"Content-Type": content_type}, content=html.encode("utf-8"))


def test_ok_page_keeps_full_text_links_title_and_final_url(monkeypatch):
    long_text = "word " * 1000                       # 5,000 chars: the class function would cut at 2,000
    html = (f"<html><head><title> Acme </title></head><body><script>x()</script>"
            f"<p>{long_text}</p><a href='/about'>About</a><a>no href</a></body></html>")
    monkeypatch.setattr(scraper.requests, "get",
                        lambda url, headers, timeout: fake_response("https://www.acme.com/", html=html))
    page = scraper.fetch_page("https://acme.com")
    assert page["error"] is None
    assert page["final_url"] == "https://www.acme.com/"
    assert page["title"] == "Acme"
    assert page["links"] == ["/about"]
    assert len(page["text"]) > 4_000 and "x()" not in page["text"]


def test_timeout_is_reported_not_raised(monkeypatch):
    def timeout(url, headers, timeout):
        raise requests.Timeout("read timed out")
    monkeypatch.setattr(scraper.requests, "get", timeout)
    page = scraper.fetch_page("https://acme.com")
    assert page["error"].startswith("Timeout")
    assert page["text"] == "" and page["http_status"] is None


def test_http_error_page_is_not_content(monkeypatch):
    monkeypatch.setattr(scraper.requests, "get",
                        lambda url, headers, timeout: fake_response(status=404, html="<body>Not found</body>"))
    page = scraper.fetch_page("https://acme.com/missing")
    assert page["error"] == "HTTP 404" and page["text"] == ""


def test_non_html_is_rejected(monkeypatch):
    monkeypatch.setattr(scraper.requests, "get",
                        lambda url, headers, timeout: fake_response(content_type="application/pdf"))
    page = scraper.fetch_page("https://acme.com/file")
    assert "Not an HTML page" in page["error"]


def test_class_functions_are_unchanged():
    # the two original class functions are still there (fetch_page was added next to them)
    assert callable(scraper.fetch_website_contents) and callable(scraper.fetch_website_links)
