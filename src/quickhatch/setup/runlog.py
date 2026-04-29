"""Structured run artifact writer for remote setup."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|passphrase|private[_-]?key|authorized_keys)",
    re.IGNORECASE,
)
_SENSITIVE_LINE_RE = re.compile(
    r"(api[_-]?key|token|secret|password|passphrase)\s*[:=]\s*([^\s,'\"}]+)",
    re.IGNORECASE,
)
_SENSITIVE_JSON_FIELD_RE = re.compile(
    r'("(?:[^"]*(?:api[_-]?key|token|secret|password|passphrase|private[_-]?key)[^"]*)"\s*:\s*)"[^"]*"',
    re.IGNORECASE,
)
_SSH_PUBLIC_KEY_RE = re.compile(
    r"\b(?:ssh-(?:ed25519|rsa)|ecdsa-sha2-[A-Za-z0-9@._+-]+)\s+[A-Za-z0-9+/=]+"
    r"(?:\s+[^\r\n'\"`;|&<>)]*)?",
)


def redact_sensitive_text(value: str) -> str:
    text = _SENSITIVE_JSON_FIELD_RE.sub(r'\1"***REDACTED***"', value)
    text = _SENSITIVE_LINE_RE.sub(lambda m: f"{m.group(1)}=***REDACTED***", text)
    text = re.sub(
        r"echo\s+['\"][^:'\"]+:[^'\"]+['\"]\s*\|\s*chpasswd",
        "echo '***REDACTED***' | chpasswd",
        text,
        flags=re.IGNORECASE,
    )
    return _SSH_PUBLIC_KEY_RE.sub("ssh-key ***REDACTED***", text)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if _SENSITIVE_KEY_RE.search(str(key)):
                redacted[str(key)] = "***REDACTED***"
            else:
                redacted[str(key)] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


class SetupRunLogger:
    """Append-only JSONL logger for remote setup runs."""

    def __init__(self, run_id: str, *, root: Path | None = None) -> None:
        self.run_id = run_id
        self.root = root or (Path.home() / ".quickhatch" / "setup-runs")
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self.path = self.root / f"{run_id}.jsonl"

    def record(self, event_type: str, **payload: Any) -> None:
        entry = {
            "timestamp": time.time(),
            "run_id": self.run_id,
            "event_type": event_type,
            **_redact(payload),
        }
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except OSError:
            # Best-effort debugging aid — never let logging break the install.
            pass
