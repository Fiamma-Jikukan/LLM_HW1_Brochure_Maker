"""
Generation: brochures (improved and baseline) and the comparison.

Every function here:
  - takes a company (or two), an audience and a SAVED context - it never fetches pages,
  - makes exactly one model call (the client has max_retries=0),
  - shows the result (Markdown in the notebook; streamed live when stream=True),
  - saves the exact prompt, the exact context, the output text and its metadata,
  - logs the call in the run log (runlog.py),
  - returns the result.

Files written under runs/<run_id>/ (name = e.g. "acme_customers", "acme_customers_baseline",
"comparison"; a repeated call in the same run gets "<name>_attempt2", ... and overwrites nothing):
  prompts/<name>.json    the exact messages sent
  contexts/<name>.txt    the exact source block, and contexts/<name>.json with its inclusion report
  outputs/<name>.md      the output text, exactly as received
  outputs/<name>.json    metadata: run_id, model, status, finish_reason, usage, duration, error,
                         word count and the results of the code checks (checks.py)
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .checks import check_output, format_checks
from .config import AUDIENCES, MAX_TOKENS, MODEL, Company, get_client, mask_secrets
from .context import save_context
from .prompts import baseline_brochure_messages, brochure_messages, comparison_messages
from .runlog import RunLog
from .streaming import _usage_dict, accumulate_stream

# ---------------------------------------------------------------------------
# Display (Markdown in Jupyter; plain text elsewhere)
# ---------------------------------------------------------------------------


def _in_notebook() -> bool:
    try:
        from IPython import get_ipython

        shell = get_ipython()
        return shell is not None and shell.__class__.__name__ == "ZMQInteractiveShell"
    except ImportError:
        return False


class _Display:
    """Shows text that may grow over time (streaming) and its final state."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.notebook = enabled and _in_notebook()
        self.handle = None
        if self.notebook:
            from IPython.display import Markdown, display

            self.handle = display(Markdown(""), display_id=True)

    def update(self, text: str) -> None:
        if self.notebook:
            from IPython.display import Markdown

            self.handle.update(Markdown(text))

    def final(self, text: str, status: str, error: str | None) -> None:
        if not self.enabled:
            return
        note = "" if status == "complete" else f"\n\n> **[{status}]** {error or ''}"
        if self.notebook:
            from IPython.display import Markdown

            self.handle.update(Markdown(text + note))
        else:
            print(text + note)


# ---------------------------------------------------------------------------
# One model call
# ---------------------------------------------------------------------------


def run_completion(messages: list[dict], stream: bool = False, on_text=None, client=None) -> dict:
    """
    One call, no retries. Returns {text, status, finish_reason, usage, error, duration_seconds}.
    Status is "complete", "partial" or "failed" (see streaming.py); a failure is returned, not raised.
    `client` can be replaced by a fake in tests.
    """
    client = client or get_client()
    started = time.perf_counter()
    try:
        if stream:
            events = client.chat.completions.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                messages=messages,
                stream=True,
                stream_options={"include_usage": True},
            )
            result = accumulate_stream(events, on_text=on_text)
        else:
            response = client.chat.completions.create(model=MODEL, max_tokens=MAX_TOKENS, messages=messages)
            choice = response.choices[0] if response.choices else None
            text = (choice.message.content if choice else None) or ""
            finish_reason = choice.finish_reason if choice else None
            if finish_reason == "stop" and text:
                status = "complete"
            else:
                status = "partial" if text else "failed"
            result = {
                "text": text,
                "status": status,
                "finish_reason": finish_reason,
                "usage": _usage_dict(response.usage),
                "error": None,
            }
    except Exception as e:  # the request itself failed (network, 4xx/5xx, timeout)
        result = {
            "text": "",
            "status": "failed",
            "finish_reason": None,
            "usage": None,
            "error": f"{type(e).__name__}: {e}",
        }
    result["error"] = mask_secrets(result["error"])
    result["duration_seconds"] = round(time.perf_counter() - started, 2)
    return result


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _generate(name: str, messages: list[dict], context: dict, run: RunLog,
              meta: dict, stream: bool, display: bool, client=None) -> dict:
    call_key = f"{meta['action']}:{name}"
    attempt = run.next_attempt(call_key)
    stem = run.file_stem(name, attempt)
    paths = {
        "prompt": run.dir / "prompts" / f"{stem}.json",
        "context": run.dir / "contexts" / f"{stem}.json",
        "output": run.dir / "outputs" / f"{stem}.md",
        "metadata": run.dir / "outputs" / f"{stem}.json",
    }
    # Inputs are saved BEFORE the call, so they exist even if the call fails.
    _write_json(paths["prompt"], messages)
    save_context(context, paths["context"])

    shown = _Display(display)
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result = run_completion(messages, stream=stream, on_text=shown.update if stream else None, client=client)
    shown.final(result["text"], result["status"], result["error"])

    paths["output"].parent.mkdir(parents=True, exist_ok=True)
    paths["output"].write_text(result["text"], encoding="utf-8")

    checks = check_output(
        result["text"], context, kind=meta["action"], company_name=meta.get("company_name"),
        audience=meta.get("audience"), variant=meta.get("variant", "improved"),
    )
    if display:
        print(f"[checks: {name}] {format_checks(checks)}")

    record = {
        "name": name,
        "run_id": run.run_id,
        "attempt": attempt,
        **meta,
        "model": MODEL,
        "streamed": stream,
        "started_at": started_at,
        **result,
        "included_ids": context["included_ids"],
        "word_count": checks.get("word_count"),
        "checks": checks,
        "paths": {k: str(v) for k, v in paths.items()},
    }
    _write_json(paths["metadata"], {k: v for k, v in record.items() if k != "text"})

    run.log_model_call(
        call_key=call_key, attempt=attempt, action=meta["action"], company_id=meta["company_id"],
        audience=meta.get("audience"), variant=meta.get("variant"),
        duration_seconds=result["duration_seconds"], status=result["status"], error=result["error"],
        usage=result["usage"], finish_reason=result["finish_reason"],
        inputs={"prompt": paths["prompt"], "context": paths["context"]},
        outputs={"output": paths["output"], "metadata": paths["metadata"]},
    )
    return record


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def generate_brochure(company: Company, audience: str, context: dict, run: RunLog,
                      variant: str = "improved", stream: bool = False, display: bool = True,
                      client=None) -> dict:
    """
    Generate one brochure for one company and audience from a saved context.
    variant: "improved" (audience-specific structure) or "baseline" (simple prompt, same rules).
    Returns the result record (text, status, usage, paths, ...).
    """
    if audience not in AUDIENCES:
        raise ValueError(f"Unknown audience {audience!r}; expected one of {list(AUDIENCES)}.")
    if variant == "improved":
        messages = brochure_messages(company, audience, context)
        name = f"{company.company_id}_{audience}"
    elif variant == "baseline":
        messages = baseline_brochure_messages(company, audience, context)
        name = f"{company.company_id}_{audience}_baseline"
    else:
        raise ValueError(f"Unknown variant {variant!r}; expected 'improved' or 'baseline'.")
    meta = {
        "action": "brochure",
        "company_id": company.company_id,
        "company_name": company.name,
        "audience": audience,
        "variant": variant,
    }
    return _generate(name, messages, context, run, meta, stream, display, client)


def generate_comparison(company_a: Company, company_b: Company, context: dict, run: RunLog,
                        stream: bool = False, display: bool = True, client=None) -> dict:
    """Generate the comparison from ONE context built from both companies' saved collections."""
    messages = comparison_messages(company_a, company_b, context)
    meta = {
        "action": "comparison",
        "company_id": f"{company_a.company_id}+{company_b.company_id}",
        "company_name": f"{company_a.name} / {company_b.name}",
        "audience": None,
        "variant": "improved",
    }
    return _generate("comparison", messages, context, run, meta, stream, display, client)
