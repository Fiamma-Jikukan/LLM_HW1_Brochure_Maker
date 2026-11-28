"""Source collection (ok / empty / failed, IDs, replacements) and the context budget policy."""

import pytest

from brochure.config import CONTEXT_CHAR_BUDGET, MIN_TEXT_CHARS
from brochure.context import build_context, cut_text, fair_shares
from brochure.links import build_candidates
from brochure.prompts import GROUNDING_RULES, OUTPUT_RULES, brochure_messages, comparison_messages
from brochure.sources import (
    add_replacement_source,
    collect_sources,
    summarize_sources,
    usable_sources,
)

LINE = "Acme helps teams ship software faster.\n"


def text(n: int, tag: str = "") -> str:
    """n characters of page text; `tag` makes each page's lines its own (not site-wide text)."""
    line = f"{tag}{LINE}"
    return (line * (n // len(line) + 1))[:n]


def accepted(*urls):
    links = [{"type": "page", "url": u, "reason": "useful", "normalized_url": u} for u in urls]
    return {"company_id": "acme", "status": "accepted", "links": links, "rejection_reasons": []}


# ---------------------------------------------------------------------------
# Source collection
# ---------------------------------------------------------------------------

@pytest.fixture
def collection(acme, acme_home, fetch_recorder):
    pages = {
        "https://www.acme.com/about": {"text": text(3_000)},
        "https://www.acme.com/careers": {"text": "Loading..."},                   # empty: JavaScript page
        "https://www.acme.com/products": {"http_status": 500, "error": "HTTP 500"},  # failed
    }
    fetch = fetch_recorder(pages)
    col = collect_sources(acme, acme_home, accepted(*pages), fetch=fetch)
    col["_fetch_calls"] = fetch.calls
    return col


def test_records_have_ids_in_order_and_all_required_fields(collection):
    ids = [r["source_id"] for r in collection["sources"]]
    assert ids == ["A1", "A2", "A3", "A4"]                       # home first, then the model's order
    assert collection["sources"][0]["url"] == "https://www.acme.com/"
    required = {"source_id", "company_id", "url", "title", "retrieved_at", "text", "status"}
    assert all(required <= set(r) for r in collection["sources"])


def test_status_ok_empty_failed(collection):
    assert [r["status"] for r in collection["sources"]] == ["ok", "ok", "empty", "failed"]
    assert str(MIN_TEXT_CHARS) in collection["sources"][2]["error"]
    assert collection["sources"][3]["error"] == "HTTP 500"


def test_only_ok_sources_are_evidence_and_gaps_are_shown(collection):
    assert [r["source_id"] for r in usable_sources(collection)] == ["A1", "A2"]
    summary = summarize_sources(collection)
    assert "A3" in summary and "not used as evidence" in summary
    assert "usable sources: 2 of 4" in summary


def test_home_page_is_not_fetched_again(collection):
    assert "https://acme.com" not in collection["_fetch_calls"]
    assert len(collection["_fetch_calls"]) == 3
    assert collection["site_requests"] == 4                       # home (earlier) + 3 pages


def test_replacement_uses_a_free_id_and_follows_the_rules(acme, acme_home, fetch_recorder):
    cands = build_candidates(acme, acme_home)
    fetch = fetch_recorder({"https://www.acme.com/about": {"text": text(500)},
                            "https://www.acme.com/pricing": {"text": text(800)}})
    col = collect_sources(acme, acme_home, accepted("https://www.acme.com/about"), fetch=fetch)

    for bad, message in [("https://www.acme.com/privacy", "site/filter rules"),
                         ("https://www.acme.com/team", "not in the saved candidate list"),
                         ("https://www.acme.com/about", "already in the collection")]:
        with pytest.raises(ValueError, match=message):
            add_replacement_source(col, cands, bad, "test", fetch=fetch)

    record = add_replacement_source(col, cands, "https://acme.com/pricing/", "need pricing info", fetch=fetch)
    assert record["source_id"] == "A3" and record["status"] == "ok"
    assert col["replacements"] == [{"source_id": "A3", "url": "https://www.acme.com/pricing",
                                    "reason": "need pricing info"}]


def test_no_replacement_beyond_the_page_limit(collection, acme, acme_home):
    with pytest.raises(ValueError, match="no free ID"):
        add_replacement_source(collection, build_candidates(acme, acme_home),
                               "https://www.acme.com/pricing", "x")


# ---------------------------------------------------------------------------
# Context budget
# ---------------------------------------------------------------------------

def src(sid, n, status="ok"):
    return {"source_id": sid, "url": f"https://acme.com/{sid}", "title": f"Page {sid}", "text": text(n, f"{sid}: "),
            "status": status, "error": None if status == "ok" else "HTTP 404"}


def coll(letter, name, sources):
    return {"letter": letter, "company_name": name, "home_url": f"https://{name.lower()}.com", "sources": sources}


def test_fair_shares():
    assert fair_shares([500, 3_000, 50_000, 2_000], 12_000) == [500, 3_000, 6_500, 2_000]
    assert fair_shares([100, 200], 12_000) == [100, 200]
    assert sum(fair_shares([9_000, 9_000, 9_000], 10_000)) == 10_000


def test_cut_text_prefers_a_line_break():
    cut = cut_text(text(5_000), 1_000)
    assert len(cut) <= 1_000 and cut.endswith("faster.")


def test_long_source_is_shortened_without_pushing_others_out():
    c = coll("A", "Acme", [src("A1", 200_000), src("A2", 2_500), src("A3", 1_500), src("A4", 50, "empty")])
    ctx = build_context([c])
    report = {r["source_id"]: r for r in ctx["report"]}
    assert ctx["source_chars_used"] <= CONTEXT_CHAR_BUDGET
    assert report["A1"]["decision"] == "shortened"
    assert report["A2"]["decision"] == "included" and report["A2"]["used_chars"] == 2_500
    assert report["A3"]["decision"] == "included" and report["A3"]["used_chars"] == 1_500
    assert report["A4"]["decision"] == "left out" and "empty" in report["A4"]["reason"]
    assert ctx["included_ids"] == ["A1", "A2", "A3"]
    assert "[... shortened: kept" in ctx["text"]


def test_empty_and_failed_sources_are_left_out_with_reasons():
    c = coll("A", "Acme", [src("A1", 1_000), src("A2", 0, "failed"), src("A3", 10, "empty")])
    ctx = build_context([c])
    left_out = [r for r in ctx["report"] if r["decision"] == "left out"]
    assert [r["source_id"] for r in left_out] == ["A2", "A3"]
    assert all("not usable as evidence" in r["reason"] for r in left_out)
    assert 'id="A2"' not in ctx["text"] and 'id="A3"' not in ctx["text"]


def test_ids_and_headers_survive_a_tiny_budget():
    a = coll("A", "Acme", [src("A1", 50_000), src("A2", 2_500)])
    b = coll("B", "Beta", [src("B1", 9_000), src("B2", 9_000)])
    ctx = build_context([a, b], budget=600)
    assert ctx["source_chars_used"] <= 600
    for sid in ["A1", "A2", "B1", "B2"]:
        assert f'<source id="{sid}"' in ctx["text"]
    assert "## Company A: Acme" in ctx["text"] and "## Company B: Beta" in ctx["text"]


def test_comparison_shares_one_budget_between_companies():
    a = coll("A", "Acme", [src("A1", 50_000), src("A2", 2_500)])
    b = coll("B", "Beta", [src("B1", 9_000), src("B2", 9_000), src("B3", 9_000)])
    ctx = build_context([a, b])
    used = {r["source_id"]: r["used_chars"] for r in ctx["report"]}
    assert ctx["source_chars_used"] <= CONTEXT_CHAR_BUDGET
    assert abs((used["A1"] + used["A2"]) - (used["B1"] + used["B2"] + used["B3"])) < 200   # ~6,000 each


def test_instructions_stay_whole_with_a_very_long_source(acme, beta):
    c = coll("A", "Acme", [src("A1", 500_000)])
    ctx = build_context([c])
    system, user = (m["content"] for m in brochure_messages(acme, "customers", ctx))
    assert GROUNDING_RULES in system and OUTPUT_RULES in system        # never shortened
    assert user.startswith("Company: Acme")
    assert user.rstrip().endswith("following the sections and rules above.")
    assert "Source IDs you may cite: A1" in user
    assert len(user) < CONTEXT_CHAR_BUDGET + 2_000                     # budget + fixed headers only

    both = build_context([c, coll("B", "Beta", [src("B1", 500_000)])])
    system, user = (m["content"] for m in comparison_messages(acme, beta, both))
    assert GROUNDING_RULES in system
    assert user.rstrip().endswith("following the structure and rules above.")
