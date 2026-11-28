"""
Saved text files are byte-exact copies, with no newline conversion on any platform.
Source text from websites can contain "\\r\\n" and lone "\\r" (21 of them on lastprice.co.il). On Windows, plain
write_text() turns "\\n" into "\\r\\n", so "\\r\\n" became "\\r\\r\\n", and reading it back gave extra line breaks.
"""

import json
from pathlib import Path
from types import SimpleNamespace as NS

from brochure.checks import check_saved_output
from brochure.context import (
    build_context,
    read_text_exact,
    rebuild_context,
    save_context,
    write_text_exact,
)
from brochure.generate import generate_brochure
from brochure.runlog import RunLog

TRICKY = "line one\r\nline two\rline three\nקו רביעי\r\n\r\nend"


def test_write_and_read_exact_round_trip(tmp_path):
    path = tmp_path / "x.txt"
    write_text_exact(path, TRICKY)
    assert path.read_bytes() == TRICKY.encode("utf-8")        # nothing converted on disk
    assert read_text_exact(path) == TRICKY                     # nothing converted on reading


def test_saved_context_txt_and_json_are_both_exact(tmp_path):
    collection = {"letter": "B", "company_name": "Shop", "home_url": "https://shop.com", "sources": [
        {"source_id": "B1", "url": "https://shop.com/", "title": "Home", "status": "ok", "error": None,
         "text": "Welcome\r\n\t\tfounded in 2002,\r\nwe sell electronics." * 20},
    ]}
    ctx = build_context([collection])
    assert "\r" in ctx["text"]
    json_path = save_context(ctx, tmp_path / "contexts" / "shop_customers")

    assert read_text_exact(json_path.with_suffix(".txt")) == ctx["text"]
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved["text"] == ctx["text"]
    assert rebuild_context(saved, [collection])["text"] == saved["text"]    # what the replay compares


def test_generated_output_file_is_exact_and_replay_reads_it_exactly(tmp_path, acme):
    reply = ("# Acme\r\n" + " ".join(["מילה"] * 230) + " [A1]\r\n"
             "## מקורות\r\n- [A1] https://acme.com/")
    client = NS(chat=NS(completions=NS(create=lambda **kw: NS(
        choices=[NS(message=NS(content=reply), finish_reason="stop")], usage=None))))
    context = {"text": "x", "budget": 12_000, "source_chars_used": 1, "included_ids": ["A1"],
               "report": [{"source_id": "A1", "url": "https://acme.com/", "decision": "included"}]}

    run = RunLog(runs_dir=tmp_path)
    record = generate_brochure(acme, "customers", context, run, variant="baseline", display=False, client=client)
    assert Path(record["paths"]["output"]).read_bytes() == reply.encode("utf-8")
    assert read_text_exact(record["paths"]["output"]) == record["text"] == reply

    meta = json.loads(Path(record["paths"]["metadata"]).read_text(encoding="utf-8"))
    assert check_saved_output(run.dir, "acme_customers_baseline") == meta["checks"]
