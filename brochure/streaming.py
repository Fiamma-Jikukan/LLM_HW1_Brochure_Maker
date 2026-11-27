"""
Accumulating a streamed model response.

The stream is a sequence of chunks. Some carry text, some don't:
  - chunks with an empty `choices` list (e.g. the final usage-only chunk with include_usage),
  - chunks whose delta.content is None or "" (e.g. the role-only first chunk),
  - the final chunk that only sets finish_reason.
Only real text is added, so the word "None" can never end up in the output.

Status of the result:
  complete  the stream ended normally (finish_reason "stop") without an error, with some text
  partial   some text arrived, but the stream broke off (exception) or ended for another
            reason (e.g. "length" = cut off by max_tokens, or no finish_reason at all)
  failed    no text arrived at all and the stream did not end normally
The returned text is exactly the text that was accumulated - nothing is added or removed.
"""


def _get(obj, name):
    """Read a field from an SDK object or a plain dict (tests use dicts / simple objects)."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _usage_dict(usage) -> dict | None:
    if usage is None:
        return None
    return {
        "prompt_tokens": _get(usage, "prompt_tokens"),
        "completion_tokens": _get(usage, "completion_tokens"),
        "total_tokens": _get(usage, "total_tokens"),
    }


def accumulate_stream(events, on_text=None) -> dict:
    """
    Consume a stream of chunks. `on_text(text_so_far)` is called after each piece of text
    (used to update the display). Returns {text, status, finish_reason, usage, error, chunks}.
    """
    parts: list[str] = []
    finish_reason = None
    usage = None
    error = None
    chunks = 0

    try:
        for event in events:
            chunks += 1
            if _get(event, "usage"):
                usage = _usage_dict(_get(event, "usage"))
            choices = _get(event, "choices") or []
            if not choices:
                continue
            choice = choices[0]
            piece = _get(_get(choice, "delta"), "content")
            if isinstance(piece, str) and piece:
                parts.append(piece)
                if on_text is not None:
                    on_text("".join(parts))
            if _get(choice, "finish_reason"):
                finish_reason = _get(choice, "finish_reason")
    except Exception as e:  # connection dropped, timeout, server error mid-stream
        error = f"{type(e).__name__}: {e}"

    text = "".join(parts)
    if error is None and finish_reason == "stop" and text:
        status = "complete"
    elif text:
        status = "partial"
    else:
        status = "failed"
    return {
        "text": text,
        "status": status,
        "finish_reason": finish_reason,
        "usage": usage,
        "error": error,
        "chunks": chunks,
    }
