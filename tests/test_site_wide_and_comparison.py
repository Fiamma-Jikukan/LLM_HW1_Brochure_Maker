"""
Fix 1: site-wide menu text is moved / removed before the budget is applied.
Fix 2: the comparison prompt states the exact source-list format.
"""

from brochure.checks import check_output
from brochure.config import SOURCES_HEADING
from brochure.context import (
    SITE_WIDE_SEPARATOR,
    build_context,
    rebuild_context,
    site_wide_lines,
)
from brochure.prompts import comparison_messages

# A site-wide menu of ~13,000 characters, as on lastprice.co.il, repeated at the top of every page.
MENU = "\n".join(f"Department {i} | Category menu item" for i in range(400))


def page(sid, own_text, status="ok"):
    return {"source_id": sid, "url": f"https://shop.com/{sid}", "title": f"Page {sid}",
            "text": f"{MENU}\n{own_text}" if own_text is not None else "", "status": status,
            "error": None if status == "ok" else "HTTP 500"}


def company(letter="A", sources=None):
    return {"letter": letter, "company_name": "Shop", "home_url": "https://shop.com", "sources": sources}


JOBS = "We are hiring: customer service representatives and warehouse workers. Send your CV to jobs@shop.com."
ABOUT = "Shop was founded in 2010 and sells electronics online with delivery across the country."
HOME = "Welcome to Shop - the lowest prices on electronics, with a 1-year warranty on every product."


def shop(*extra):
    return company(sources=[page("A1", HOME), page("A2", JOBS), page("A3", ABOUT), *extra])


# ---------------------------------------------------------------------------
# Fix 1
# ---------------------------------------------------------------------------

def test_lines_on_every_usable_page_are_site_wide():
    sources = shop()["sources"]
    shared = site_wide_lines(sources)
    assert "Department 0 | Category menu item" in shared
    assert JOBS not in shared and HOME not in shared
    assert site_wide_lines(sources[:1]) == set()            # one page: nothing is site-wide


def test_without_the_fix_the_menu_pushes_the_real_content_out():
    ctx = build_context([shop()], remove_site_wide=False)
    assert JOBS not in ctx["text"] and ABOUT not in ctx["text"]   # the problem seen in the real run
    assert ctx["site_wide_removal"] is False


def test_with_the_fix_the_real_content_reaches_the_model():
    ctx = build_context([shop()])
    assert JOBS in ctx["text"] and ABOUT in ctx["text"] and HOME in ctx["text"]
    assert ctx["site_wide_removal"] is True
    assert ctx["source_chars_used"] <= ctx["budget"]


def test_home_keeps_the_menu_after_its_own_text_other_pages_lose_it():
    ctx = build_context([shop()])
    blocks = {b.split('"')[1]: b for b in ctx["text"].split("<source id=")[1:]}
    home = blocks["A1"]
    assert home.index(HOME) < home.index(SITE_WIDE_SEPARATOR)     # own text first, menu after
    assert "Department 0 | Category menu item" in home
    for sid in ("A2", "A3"):
        assert "Category menu item" not in blocks[sid]

    report = {r["source_id"]: r for r in ctx["report"]}
    assert "moved to the end" in report["A1"]["reason"]
    assert "removed" in report["A2"]["reason"] and report["A2"]["site_wide_chars"] > 10_000
    assert report["A2"]["decision"] == "included"
    assert report["A2"]["available_chars"] == len(JOBS)


def test_a_failed_page_does_not_stop_the_menu_from_being_found():
    # the failed page has no text; if it counted, no line would be "on every page"
    ctx = build_context([shop(page("A4", None, status="failed"))])
    assert JOBS in ctx["text"]
    a4 = next(r for r in ctx["report"] if r["source_id"] == "A4")
    assert a4["decision"] == "left out" and "failed" in a4["reason"]


def test_a_page_that_is_only_menu_is_left_out_with_a_reason():
    ctx = build_context([shop(page("A4", ""))])
    a4 = next(r for r in ctx["report"] if r["source_id"] == "A4")
    assert a4["decision"] == "left out"
    assert "only site-wide menu text" in a4["reason"] and "A1" in a4["reason"]
    assert 'id="A4"' not in ctx["text"]
    assert "A4" not in ctx["included_ids"]


def test_a_company_with_one_usable_page_is_unchanged():
    single = company(sources=[page("A1", HOME), page("A2", None, status="failed")])
    on, off = build_context([single]), build_context([single], remove_site_wide=False)
    assert on["text"] == off["text"]
    assert on["report"][0]["site_wide_chars"] == 0


def test_each_company_is_handled_separately_in_the_comparison():
    a = shop()
    b = company("B", [{**page("B1", "Beta home page text about laptops."), "source_id": "B1"},
                      {**page("B2", "Beta careers: software developers wanted."), "source_id": "B2"}])
    ctx = build_context([a, b])
    assert JOBS in ctx["text"] and "Beta careers: software developers wanted." in ctx["text"]
    assert ctx["source_chars_used"] <= ctx["budget"]


def test_saved_sources_are_not_changed():
    collection = shop()
    before = [s["text"] for s in collection["sources"]]
    build_context([collection])
    assert [s["text"] for s in collection["sources"]] == before


def test_replay_rebuilds_with_the_settings_the_context_was_built_with():
    collection = shop()
    new = build_context([collection])
    assert rebuild_context(new, [collection])["text"] == new["text"]

    old = build_context([collection], remove_site_wide=False)
    legacy = {k: v for k, v in old.items() if k != "site_wide_removal"}   # saved before the setting existed
    assert rebuild_context(legacy, [collection])["text"] == old["text"]


# ---------------------------------------------------------------------------
# Fix 2
# ---------------------------------------------------------------------------

def test_comparison_prompt_states_the_source_list_format(acme, beta):
    ctx = build_context([shop()])
    system = comparison_messages(acme, beta, ctx)[0]["content"]
    assert f'"{SOURCES_HEADING}"' in system
    assert '"- [A1] https://..."' in system


def test_the_format_seen_in_the_run_fails_and_the_requested_format_passes():
    ctx = {"included_ids": ["A1", "B1"], "report": [
        {"source_id": "A1", "url": "https://a.com/", "decision": "included"},
        {"source_id": "B1", "url": "https://b.com/", "decision": "included"}]}
    table = ("| ממד | A | B |\n|---|---|---|\n| מוצרים ושירותים | x [A1] | y [B1] |\n| קהלי יעד | x [A1] | y [B1] |\n"
             "| תמחור או תקופת ניסיון | x [A1] | y [B1] |\n| עבודה בחברה | x [A1] | y [B1] |\n")
    seen_in_run = table + "## מקורות\n- A1: https://a.com/\n- B1: https://b.com/"
    requested = table + "## מקורות\n- [A1] https://a.com/\n- [B1] https://b.com/"
    assert not check_output(seen_in_run, ctx, kind="comparison")["passed"]
    assert check_output(requested, ctx, kind="comparison")["passed"]
