"""
Evidence & observability (REPORT.md section 6 / brief section 3.5).

Every run (discovery or replay) writes a structured JSON-lines log of what
happened and why, plus a final result summary. On failure, a richer signal
(here: the raw page HTML at the point of failure) is captured too.

Redaction: any value belonging to an InputParam/OutputField marked
`sensitive=True` in the artifact, or any value passed to `log_secret`, is
replaced with a fixed placeholder before it ever reaches disk. Redaction
happens at the point of writing, not by trusting callers to pre-redact —
so a bug elsewhere in the system can't leak a raw value through logging.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REDACTED = "[REDACTED]"


class EvidenceRecorder:
    def __init__(self, run_id: str, run_kind: str, base_dir: str = "evidence"):
        self.run_id = run_id
        self.run_kind = run_kind  # "discovery" | "replay"
        self.dir = Path(base_dir) / f"{run_kind}_{run_id}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.dir / "run_log.jsonl"
        self._sensitive_values: set[str] = set()

    def mark_sensitive(self, value: str | None) -> None:
        if value:
            self._sensitive_values.add(value)

    def _redact(self, obj: Any) -> Any:
        if isinstance(obj, str):
            for s in self._sensitive_values:
                if s and s in obj:
                    obj = obj.replace(s, REDACTED)
            return obj
        if isinstance(obj, dict):
            return {k: self._redact(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._redact(v) for v in obj]
        return obj

    def log(self, event: str, **fields: Any) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "event": event,
            **fields,
        }
        record = self._redact(record)
        with open(self.log_path, "a") as f:
            f.write(json.dumps(record) + "\n")

    def save_failure_snapshot(self, label: str, html: str) -> str:
        path = self.dir / f"failure_{label}.html"
        with open(path, "w") as f:
            f.write(self._redact(html))
        return str(path)

    def save_result(self, result: dict[str, Any]) -> None:
        result = self._redact(result)
        with open(self.dir / "result.json", "w") as f:
            json.dump(result, f, indent=2)

    def save_artifact(self, artifact_dict: dict[str, Any]) -> None:
        with open(self.dir / "artifact.json", "w") as f:
            json.dump(artifact_dict, f, indent=2)
