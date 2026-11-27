"""Streaming accumulation with fake event sequences, and generation with fake clients."""

import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from brochure.generate import generate_brochure, run_completion
from brochure.runlog import RunLog
from brochure.streaming import accumulate_stream


def chunk(content=None, finish=None):
    return NS(choices=[NS(delta=NS(content=content), finish_reason=finish)], usage=None)


def usage_chunk():
    return NS(choices=[], usage=NS(prompt_tokens=10, completion_tokens=3, total_tokens=13))


def broken_after(events, n):
    for i, event in enumerate(events):
        if i == n:
            raise ConnectionError("connection reset by peer")
        yield event


NORMAL = [
    chunk(None),            # role-only first chunk: delta.content is None
    chunk(""),              # empty text
    chunk("שלום "),
    NS(choices=[NS(delta=None, finish_reason=None)], usage=None),   # no delta at all
    chunk("עולם"),
    chunk(None, "stop"),    # finish chunk without text
    usage_chunk(),          # include_usage chunk: empty choices list
]


def test_mock_sequence_saves_exactly_the_accumulated_text():
    updates = []
    result = accumulate_stream(NORMAL, on_text=updates.append)
    assert result["text"] == "שלום עולם"
    assert result["status"] == "complete"
    assert "None" not in result["text"]
    assert updates == ["שלום ", "שלום עולם"]                     # shown step by step
    assert result["usage"] == {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}


def test_stream_broken_mid_way_is_partial():
    result = accumulate_stream(broken_after(NORMAL, 4))
    assert result["status"] == "partial"
    assert result["text"] == "שלום "
    assert "ConnectionError" in result["error"]


def test_stream_broken_before_any_text_is_failed():
    result = accumulate_stream(broken_after(NORMAL, 1))
    assert result["status"] == "failed" and result["text"] == ""


@pytest.mark.parametrize("events, status", [
    ([chunk("abc"), chunk(None, "length")], "partial"),         # cut by max_tokens
    ([chunk("abc"), chunk("def")], "partial"),                  # never finished
    ([chunk(None), chunk(None, "stop")], "failed"),             # finished but no text
    ([NS(choices=None, usage=None), chunk("x", "stop")], "complete"),
])
def test_status_rules(events, status):
    result = accumulate_stream(events)
    assert result["status"] == status
    assert "None" not in result["text"]


def test_dict_shaped_events_also_work():
    events = [{"choices": [{"delta": {"content": "x"}, "finish_reason": None}]},
              {"choices": [{"delta": {}, "finish_reason": "stop"}]}, {"choices": []}]
    assert accumulate_stream(events)["text"] == "x"


# ---------------------------------------------------------------------------
# run_completion / generate_brochure with fake clients
# ---------------------------------------------------------------------------

class FakeClient:
    def __init__(self, response=None, exc=None, stream_events=None):
        self.chat = NS(completions=NS(create=self.create))
        self.response, self.exc, self.stream_events = response, exc, stream_events
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.exc:
            raise self.exc
        return self.stream_events if kwargs.get("stream") else self.response


def response(content, finish="stop", usage=None):
    return NS(choices=[NS(message=NS(content=content), finish_reason=finish)], usage=usage)


@pytest.mark.parametrize("client, status, text", [
    (FakeClient(response("טקסט")), "complete", "טקסט"),
    (FakeClient(response(None)), "failed", ""),
    (FakeClient(NS(choices=[], usage=None)), "failed", ""),
    (FakeClient(response("abc", finish="length")), "partial", "abc"),
    (FakeClient(exc=TimeoutError("timed out")), "failed", ""),
])
def test_run_completion_never_raises_and_never_writes_none(client, status, text):
    result = run_completion([{"role": "user", "content": "x"}], client=client)
    assert result["status"] == status and result["text"] == text


def test_only_supported_parameters_are_sent():
    client = FakeClient(stream_events=iter(NORMAL))
    run_completion([{"role": "user", "content": "x"}], stream=True, client=client)
    assert set(client.kwargs) == {"model", "max_tokens", "messages", "stream", "stream_options"}
    assert client.kwargs["stream_options"] == {"include_usage": True}


@pytest.fixture
def context():
    return {"text": '<source id="A1" url="https://acme.com/" title="Acme">x</source>',
            "budget": 12_000, "source_chars_used": 1, "included_ids": ["A1"],
            "report": [{"source_id": "A1", "url": "https://acme.com/", "decision": "included",
                        "full_chars": 1, "used_chars": 1, "reason": "fits"}]}


def test_streamed_brochure_saves_exact_text_and_marks_partial(tmp_path, acme, context):
    run = RunLog(runs_dir=tmp_path)
    client = FakeClient(stream_events=broken_after(NORMAL, 4))
    record = generate_brochure(acme, "customers", context, run, stream=True, display=False, client=client)

    saved = Path(record["paths"]["output"]).read_text(encoding="utf-8")
    assert saved == record["text"] == "שלום "
    meta = json.loads(Path(record["paths"]["metadata"]).read_text(encoding="utf-8"))
    assert meta["status"] == "partial" and meta["streamed"] is True
    assert json.loads(Path(record["paths"]["prompt"]).read_text(encoding="utf-8"))[0]["role"] == "system"
    assert Path(record["paths"]["context"]).with_suffix(".txt").read_text(encoding="utf-8") == context["text"]


def test_generation_without_a_key_fails_cleanly(tmp_path, acme, context):
    # no fake client and no API key (removed in conftest): the call fails, nothing crashes
    run = RunLog(runs_dir=tmp_path)
    record = generate_brochure(acme, "customers", context, run, display=False)
    assert record["status"] == "failed" and record["text"] == ""
    assert "ANTHROPIC_API_KEY" in record["error"]
