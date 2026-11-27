"""Candidate links (built in code) and validation of the model's link selection."""

import json

import pytest

from brochure.links import (
    build_candidates,
    links_to_fetch,
    normalize_url,
    select_links,
    validate_selection,
)
from brochure.sources import collect_sources

BASE = "https://www.acme.com/"
HOSTS = {"acme.com"}


# ---------------------------------------------------------------------------
# normalize_url / build_candidates
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("href, expected", [
    ("/about", "https://www.acme.com/about"),
    ("about/", "https://www.acme.com/about"),
    ("https://acme.com/about#team", "https://www.acme.com/about"),     # fragment removed, no-www = www
    ("http://www.acme.com/about", "https://www.acme.com/about"),       # scheme follows the home page
    ("//acme.com//about//", "https://www.acme.com/about"),             # double and trailing slashes
    ("/products?id=2", "https://www.acme.com/products?id=2"),          # query kept
    ("/photos", "https://www.acme.com/photos"),                        # contains "tos" but is not terms
])
def test_valid_links_get_one_canonical_form(href, expected):
    assert normalize_url(href, BASE, HOSTS) == (expected, None)


@pytest.mark.parametrize("href, reason_part", [
    ("", "empty"),
    ("   ", "empty"),
    (None, "empty"),
    ("mailto:info@acme.com", "mailto"),
    ("tel:+972-3-1234567", "tel"),
    ("javascript:void(0)", "javascript"),
    ("ftp://acme.com/file", "ftp"),
    ("https://jobs.acme.com/open", "outside the site"),
    ("https://linkedin.com/company/acme", "outside the site"),
    ("https://acme.com.evil.io/about", "outside the site"),
    ("/", "home page"),
    ("https://acme.com/", "home page"),
    ("/?lang=he", "home page"),
    ("#top", "home page"),
    ("/privacy-policy", "privacy"),
    ("/Terms-of-Service", "privacy"),
    ("/legal", "privacy"),
    ("/cookies", "privacy"),
    ("/%D7%AA%D7%A0%D7%90%D7%99-%D7%A9%D7%99%D7%9E%D7%95%D7%A9", "privacy"),   # Hebrew "terms of use"
    ("/brochure.pdf", "file"),
])
def test_invalid_links_are_dropped_with_a_reason(href, reason_part):
    url, reason = normalize_url(href, BASE, HOSTS)
    assert url is None
    assert reason_part in reason


def test_build_candidates_dedupes_and_records_every_dropped_link(acme):
    home = {"final_url": BASE, "links": ["/about", "about/", "http://acme.com/about#x", "mailto:a@b.c", "/careers"]}
    result = build_candidates(acme, home)
    assert result["candidates"] == ["https://www.acme.com/about", "https://www.acme.com/careers"]
    assert result["site_hosts"] == ["acme.com"]
    assert len(result["dropped"]) == 3
    assert sum("duplicate" in d["reason"] for d in result["dropped"]) == 2


# ---------------------------------------------------------------------------
# validate_selection: mock model outputs
# ---------------------------------------------------------------------------

def item(url, type_="about", reason="Useful for customers"):
    return {"type": type_, "url": url, "reason": reason}


def dumps(*items):
    return json.dumps({"links": list(items)})


@pytest.fixture
def candidates(acme, acme_home):
    return build_candidates(acme, acme_home)


def test_candidate_fixture(candidates):
    assert candidates["candidates"] == [
        "https://www.acme.com/about", "https://www.acme.com/careers",
        "https://www.acme.com/products", "https://www.acme.com/pricing",
    ]


def test_valid_selection_is_accepted_and_normalized(candidates):
    raw = dumps(item("https://www.acme.com/about"), item("https://acme.com/careers/", "careers"))
    result = validate_selection(raw, candidates)
    assert result["valid"] and result["errors"] == []
    assert [x["normalized_url"] for x in result["links"]] == [
        "https://www.acme.com/about", "https://www.acme.com/careers"]


def test_empty_selection_is_valid(candidates):
    assert validate_selection(dumps(), candidates)["valid"]


BAD_OUTPUTS = {
    "malformed JSON": ('{"links": [{"type": "about", "url": ', "invalid JSON"),
    "URL not in the input list": (
        dumps(item("https://www.acme.com/about"), item("https://www.acme.com/team")),
        "not in the candidate list"),
    "duplicate after normalization": (
        dumps(item("https://www.acme.com/about"), item("http://acme.com/about/#top")),
        "duplicates item 1"),
    "forbidden item in a partly valid list": (
        dumps(item("https://www.acme.com/about"), item("https://www.acme.com/privacy", "privacy")),
        "privacy"),
    "home page selected": (dumps(item("https://www.acme.com/")), "home page"),
    "another site": (dumps(item("https://linkedin.com/company/acme")), "outside the site"),
    "more than 3 links": (
        dumps(*(item(u) for u in ["https://www.acme.com/about", "https://www.acme.com/careers",
                                   "https://www.acme.com/products", "https://www.acme.com/pricing"])),
        "at most 3"),
    "empty reason": (dumps(item("https://www.acme.com/about", reason="  ")), "non-empty string"),
    "missing field": (json.dumps({"links": [{"type": "about", "url": "https://www.acme.com/about"}]}),
                      "missing fields"),
    "extra field": (json.dumps({"links": [{**item("https://www.acme.com/about"), "score": 9}]}),
                    "unexpected fields"),
    "field is not a string": (json.dumps({"links": [{**item("https://www.acme.com/about"), "type": 3}]}),
                              "non-empty string"),
    "missing 'links'": (json.dumps({"pages": []}), "missing field 'links'"),
    "'links' not a list": (json.dumps({"links": "https://www.acme.com/about"}), "not a list"),
    "top level not an object": (json.dumps([item("https://www.acme.com/about")]), "not a JSON object"),
    "empty response": ("", "empty response"),
}


@pytest.mark.parametrize("raw, expected_error", BAD_OUTPUTS.values(), ids=BAD_OUTPUTS.keys())
def test_bad_output_rejects_the_whole_response(raw, expected_error, candidates):
    result = validate_selection(raw, candidates)
    assert not result["valid"]
    assert result["links"] == []                       # nothing kept, even the valid items
    assert any(expected_error in e for e in result["errors"])


# ---------------------------------------------------------------------------
# A rejected response stops fetching
# ---------------------------------------------------------------------------

def fake_call(raw, finish_reason="stop", error=None):
    def call(messages):
        return {"model": "fake", "raw_output": raw, "finish_reason": finish_reason, "usage": None,
                "error": error, "duration_seconds": 0.0}
    return call


@pytest.mark.parametrize("raw, expected_error", BAD_OUTPUTS.values(), ids=BAD_OUTPUTS.keys())
def test_rejected_selection_fetches_no_page(raw, expected_error, acme, acme_home, candidates, fetch_recorder):
    selection = select_links(acme, candidates, call=fake_call(raw))
    assert selection["status"] == "rejected"
    assert selection["raw_output"] == raw                  # the raw output is kept for the record
    assert any(expected_error in r for r in selection["rejection_reasons"])

    fetch = fetch_recorder({})
    with pytest.raises(ValueError, match="no pages are fetched"):
        collect_sources(acme, acme_home, selection, fetch=fetch)
    assert fetch.calls == []                               # not a single request was made


def test_failed_call_and_cut_off_response_are_not_accepted(acme, candidates):
    failed = select_links(acme, candidates, call=fake_call(None, error="APITimeoutError: timed out"))
    assert failed["status"] == "call_failed"
    cut = select_links(acme, candidates, call=fake_call(dumps(item("https://www.acme.com/about")), "length"))
    assert cut["status"] == "rejected"
    for selection in (failed, cut):
        with pytest.raises(ValueError):
            links_to_fetch(selection)


def test_accepted_selection_keeps_the_model_order(acme, candidates):
    raw = dumps(item("https://www.acme.com/products", "products"), item("https://www.acme.com/about"))
    selection = select_links(acme, candidates, call=fake_call(raw))
    assert selection["status"] == "accepted"
    assert [x["normalized_url"] for x in links_to_fetch(selection)] == [
        "https://www.acme.com/products", "https://www.acme.com/about"]
    # the exact prompt is kept, and it lists the saved candidates
    assert "https://www.acme.com/pricing" in selection["messages"][1]["content"]
