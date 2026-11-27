"""
Run log: one folder per run, runs/<run_id>/, with a JSON-lines log (log.jsonl).

Every model call and every request to a company website is one line. Model calls record:
  run_id, timestamp, company, audience, action, variant, model, attempt, duration,
  success / status, error, input paths (prompt, context, candidates), output paths,
  and token counts ONLY when the API reported them (otherwise the field is absent).
Website requests are logged separately (kind "site_request") and counted separately.

Attempts: the client never retries on its own (max_retries=0). If we call the same thing
again in the same run (e.g. regenerate a brochure), it is logged as attempt 2, 3, ...
and its files get an "_attempt<N>" suffix, so nothing earlier is overwritten.

No secrets: the API key is never written; as a safety net, any line that would contain
the key's value is masked before it is written.
"""

import json
import secrets
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .config import (
    CONTEXT_CHAR_BUDGET,
    MAX_EXTRA_PAGES,
    MAX_TOKENS,
    MAX_WORDS,
    MIN_WORDS,
    MODEL,
    RUNS_DIR,
    mask_secrets,
)

PLANNED_MODEL_CALLS = 8   # 2 link selections + 4 brochures + 1 comparison + 1 baseline


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_run_id() -> str:
    """Sortable and unique, e.g. 20261127-121530-a3f9."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


class RunLog:
    """One run: its ID, its folder and its log. Pass an existing run_id to continue / replay a run."""

    def __init__(self, run_id: str | None = None, runs_dir: Path = RUNS_DIR):
        self.run_id = run_id or new_run_id()
        self.dir = Path(runs_dir) / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.dir / "log.jsonl"
        self._attempts: Counter = Counter()
        for entry in self.entries():          # continuing a run: rebuild attempt counters
            if entry.get("kind") == "model_call":
                self._attempts[entry["call_key"]] = max(self._attempts[entry["call_key"]], entry["attempt"])
        manifest = self.dir / "run.json"
        if not manifest.exists():
            manifest.write_text(json.dumps({
                "run_id": self.run_id,
                "created_at": _now(),
                "model": MODEL,
                "max_tokens": MAX_TOKENS,
                "context_char_budget": CONTEXT_CHAR_BUDGET,
                "max_extra_pages": MAX_EXTRA_PAGES,
                "word_range": [MIN_WORDS, MAX_WORDS],
                "max_retries": 0,
            }, ensure_ascii=False, indent=2), encoding="utf-8")

    # -- paths ---------------------------------------------------------------

    def path(self, *parts: str) -> Path:
        """A path inside the run folder (parent folders are created)."""
        p = self.dir.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def rel(self, path) -> str | None:
        """Path relative to the run folder, for the log (None stays None)."""
        if path is None:
            return None
        path = Path(path)
        try:
            return str(path.relative_to(self.dir)).replace("\\", "/")
        except ValueError:
            return str(path)

    # -- attempts ------------------------------------------------------------

    def next_attempt(self, call_key: str) -> int:
        self._attempts[call_key] += 1
        return self._attempts[call_key]

    @staticmethod
    def file_stem(name: str, attempt: int) -> str:
        return name if attempt == 1 else f"{name}_attempt{attempt}"

    # -- writing -------------------------------------------------------------

    def _write(self, entry: dict) -> dict:
        entry = {"run_id": self.run_id, "timestamp": _now(), **entry}
        line = mask_secrets(json.dumps(entry, ensure_ascii=False))   # safety net: never write the key
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return entry

    def log_model_call(self, *, call_key: str, attempt: int, action: str, company_id: str | None,
                       audience: str | None, variant: str | None, duration_seconds: float | None,
                       status: str, error: str | None, usage: dict | None,
                       inputs: dict, outputs: dict, finish_reason: str | None = None) -> dict:
        entry = {
            "kind": "model_call",
            "call_key": call_key,
            "attempt": attempt,
            "action": action,
            "company_id": company_id,
            "audience": audience,
            "variant": variant,
            "model": MODEL,
            "duration_seconds": duration_seconds,
            "success": status in ("complete", "accepted"),
            "status": status,
            "finish_reason": finish_reason,
            "error": error,
            "inputs": {k: self.rel(v) for k, v in inputs.items()},
            "outputs": {k: self.rel(v) for k, v in outputs.items()},
        }
        if usage and any(v is not None for v in usage.values()):
            entry["usage"] = {k: v for k, v in usage.items() if v is not None}
        return self._write(entry)

    def log_site_request(self, *, company_id: str, url: str, purpose: str, duration_seconds: float,
                         http_status: int | None, final_url: str | None, error: str | None) -> dict:
        return self._write({
            "kind": "site_request",
            "company_id": company_id,
            "purpose": purpose,
            "url": url,
            "final_url": final_url,
            "http_status": http_status,
            "success": error is None,
            "error": error,
            "duration_seconds": duration_seconds,
        })

    # -- reading -------------------------------------------------------------

    def entries(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def summary(self) -> dict:
        entries = self.entries()
        calls = [e for e in entries if e["kind"] == "model_call"]
        sites = [e for e in entries if e["kind"] == "site_request"]
        tokens = defaultdict(int)
        calls_with_usage = 0
        for e in calls:
            if "usage" in e:
                calls_with_usage += 1
                for k, v in e["usage"].items():
                    tokens[k] += v
        return {
            "run_id": self.run_id,
            "model_calls": len(calls),
            "planned_model_calls": PLANNED_MODEL_CALLS,
            "model_calls_by_action": dict(Counter(e["action"] for e in calls)),
            "repeated_attempts": sum(1 for e in calls if e["attempt"] > 1),
            "failed_model_calls": sum(1 for e in calls if not e["success"]),
            "model_seconds": round(sum(e["duration_seconds"] or 0 for e in calls), 2),
            "tokens_reported": dict(tokens),
            "calls_with_token_data": calls_with_usage,
            "site_requests": len(sites),
            "site_requests_by_company": dict(Counter(e["company_id"] for e in sites)),
            "failed_site_requests": sum(1 for e in sites if not e["success"]),
        }

    def format_summary(self) -> str:
        s = self.summary()
        lines = [
            f"Run {s['run_id']}",
            f"  model calls: {s['model_calls']} (planned {s['planned_model_calls']}) "
            f"by action {s['model_calls_by_action']}; failed {s['failed_model_calls']}; "
            f"repeated attempts {s['repeated_attempts']}; total {s['model_seconds']}s",
            f"  tokens (reported by the API, {s['calls_with_token_data']} calls): {s['tokens_reported']}",
            f"  requests to company sites: {s['site_requests']} {s['site_requests_by_company']}; "
            f"failed {s['failed_site_requests']}",
        ]
        return "\n".join(lines)

    def save_summary(self) -> Path:
        path = self.path("summary.json")
        path.write_text(json.dumps(self.summary(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path
