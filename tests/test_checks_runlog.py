"""Output checks (citations, source list, word count, structure) and the run log."""

import json
from pathlib import Path
from types import SimpleNamespace as NS

from brochure.checks import check_output, check_saved_output, cited_ids, count_words, expected_headings
from brochure.config import mask_secrets
from brochure.generate import generate_brochure
from brochure.runlog import RunLog

CTX = {"included_ids": ["A1", "A2"], "report": [
    {"source_id": "A1", "url": "https://acme.com/", "decision": "included"},
    {"source_id": "A2", "url": "https://acme.com/about", "decision": "shortened"},
    {"source_id": "A3", "url": "https://acme.com/careers", "decision": "left out"},
]}
WORDS = " ".join(["מילה"] * 240)
SOURCES = "## מקורות\n- [A1] https://acme.com/\n- [A2] https://acme.com/about"


def problems(text, **kw):
    return check_output(text, CTX, kind=kw.pop("kind", "brochure"), **kw)["problems"]


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def test_citation_forms_and_word_count():
    assert cited_ids("a [A1][A3]. b [A2, A4] c [B1;B2] d [a1] [12]") == ["A1", "A2", "A3", "A4", "B1", "B2"]
    assert count_words("one two three\n## מקורות\n- [A1] https://x") == 3   # source list not counted


def test_good_brochure_passes():
    assert problems(f"{WORDS} [A1][A2]\n{SOURCES}") == []


def test_cited_id_must_be_in_this_calls_context():
    found = problems(f"{WORDS} [A1] [A3] [A9]\n{SOURCES}")
    assert any("['A3', 'A9']" in p for p in found)          # A3 was left out, A9 never existed


def test_source_list_problems():
    assert any("no source list" in p for p in problems(f"{WORDS} [A1]"))
    wrong_url = f"{WORDS} [A1][A2]\n## מקורות\n- [A1] https://acme.com/\n- [A2] https://acme.com/pricing"
    assert any("source list URL for A2" in p for p in problems(wrong_url))
    not_listed = f"{WORDS} [A1][A2]\n## מקורות\n- [A1] https://acme.com/"
    assert any("missing from the source list: ['A2']" in p for p in problems(not_listed))


def test_word_count_range():
    assert any("outside 200-300" in p for p in problems(" ".join(["מילה"] * 50) + f" [A1]\n{SOURCES}"))
    assert any("outside 200-300" in p for p in problems(" ".join(["מילה"] * 320) + f" [A1]\n{SOURCES}"))


def test_no_citations_code_block_or_none():
    found = problems(f"{WORDS} None\n```x```\n{SOURCES}")
    assert {"no citations in the text", "contains a code block", "contains the literal word 'None'"} <= set(found)


def test_required_headings_for_the_improved_variant():
    expected = expected_headings("Acme", "candidates")
    assert expected[0] == "# Acme: מידע למועמדים לעבודה" and len(expected) == 5
    body = "\n".join(expected[:3] + [WORDS + " [A1]"])          # last two headings missing
    found = problems(f"{body}\n{SOURCES}", company_name="Acme", audience="candidates")
    assert any("missing required headings" in p for p in found)
    # the baseline variant has no required structure
    assert problems(f"{body}\n{SOURCES}", company_name="Acme", audience="candidates", variant="baseline") == []


def test_comparison_table_needs_all_four_dimensions():
    table = "| ממד | A | B |\n|---|---|---|\n| מוצרים ושירותים | x [A1] | y [A2] |\n| קהלי יעד | x | y |\n"
    found = problems(table + SOURCES, kind="comparison")
    assert any("missing dimensions" in p for p in found)


# ---------------------------------------------------------------------------
# run log
# ---------------------------------------------------------------------------

class FakeClient:
    def __init__(self, text="", usage=None, exc=None):
        self.chat = NS(completions=NS(create=self.create))
        self.text, self.usage, self.exc = text, usage, exc

    def create(self, **kwargs):
        if self.exc:
            raise self.exc
        return NS(choices=[NS(message=NS(content=self.text), finish_reason="stop")], usage=self.usage)


CONTEXT = {"text": "x", "budget": 12_000, "source_chars_used": 1, "included_ids": ["A1", "A2"],
           "report": CTX["report"]}


def test_every_call_is_logged_with_attempts_and_tokens_only_when_reported(tmp_path, acme):
    run = RunLog(runs_dir=tmp_path)
    usage = NS(prompt_tokens=100, completion_tokens=50, total_tokens=150)
    good = f"{WORDS} [A1][A2]\n{SOURCES}"
    first = generate_brochure(acme, "customers", CONTEXT, run, variant="baseline", display=False,
                              client=FakeClient(good, usage))
    second = generate_brochure(acme, "customers", CONTEXT, run, variant="baseline", display=False,
                               client=FakeClient(good, None))
    assert (first["attempt"], second["attempt"]) == (1, 2)
    assert Path(second["paths"]["output"]).name == "acme_customers_baseline_attempt2.md"
    assert Path(first["paths"]["output"]).exists()               # attempt 1 was not overwritten

    calls = [e for e in run.entries() if e["kind"] == "model_call"]
    assert [c["attempt"] for c in calls] == [1, 2]
    assert calls[0]["usage"] == {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    assert "usage" not in calls[1]                               # no invented token numbers
    assert calls[0]["inputs"]["prompt"] == "prompts/acme_customers_baseline.json"
    assert calls[0]["outputs"]["output"] == "outputs/acme_customers_baseline.md"
    assert {"run_id", "timestamp", "company_id", "audience", "action", "model", "duration_seconds",
            "success"} <= set(calls[0])

    summary = run.summary()
    assert summary["model_calls"] == 2 and summary["repeated_attempts"] == 1
    assert summary["calls_with_token_data"] == 1

    # replay: checks re-run from saved files only give the same result
    meta = json.loads(Path(first["paths"]["metadata"]).read_text(encoding="utf-8"))
    assert check_saved_output(run.dir, "acme_customers_baseline") == meta["checks"]

    # reopening the run continues the attempt numbers
    third = generate_brochure(acme, "customers", CONTEXT, RunLog(run.run_id, runs_dir=tmp_path),
                              variant="baseline", display=False, client=FakeClient(good))
    assert third["attempt"] == 3


def test_site_requests_are_counted_separately(tmp_path):
    run = RunLog(runs_dir=tmp_path)
    run.log_site_request(company_id="acme", url="https://acme.com", purpose="home page",
                         duration_seconds=0.1, http_status=200, final_url="https://acme.com/", error=None)
    run.log_site_request(company_id="acme", url="https://acme.com/x", purpose="selected page A2",
                         duration_seconds=0.1, http_status=500, final_url=None, error="HTTP 500")
    summary = run.summary()
    assert summary["site_requests"] == 2 and summary["failed_site_requests"] == 1
    assert summary["model_calls"] == 0


def test_the_api_key_never_reaches_a_file(tmp_path, acme, monkeypatch):
    key = "sk-ant-test-0000-FAKE"
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    assert mask_secrets(f"bad key {key}") == "bad key ***"
    run = RunLog(runs_dir=tmp_path)
    generate_brochure(acme, "customers", CONTEXT, run, display=False,
                      client=FakeClient(exc=RuntimeError(f"auth failed for {key}")))
    for path in run.dir.rglob("*"):
        if path.is_file():
            assert key not in path.read_text(encoding="utf-8"), path
