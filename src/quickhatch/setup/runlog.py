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
        r"\b(qh_pw|password|root_pw|user_pw)=('(?:[^']*)'|\"(?:[^\"]*)\"|[^\s;]+)",
        r"\1=***REDACTED***",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"echo\s+['\"][^:'\"]+:[^'\"]+['\"]\s*\|\s*chpasswd",
        "echo '***REDACTED***' | chpasswd",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?:echo|printf)\s+[^\n|]+?\|\s*(?:[^\n;|]*\s)?chpasswd",
        "echo '***REDACTED***' | chpasswd",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?:echo|printf)\s+[^\n|]+?\|\s*(?:sudo|\$[A-Za-z_][A-Za-z0-9_]*)?\s*"
        r"(?:chroot\s+(?:\"[^\"]+\"|'[^']+'|\S+)\s+)?chpasswd",
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
        self._event_counter = 0

    def record(self, event_type: str, **payload: Any) -> None:
        self._event_counter += 1
        entry = {
            "timestamp": time.time(),
            "run_id": self.run_id,
            "event_id": f"{self.run_id}:{self._event_counter:06d}",
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


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                events.append(item)
    except OSError:
        return []
    return events


def _shell_comment(text: str) -> str:
    return "\n".join(f"# {line}" if line else "#" for line in str(text).splitlines())


def build_executed_commands_script(
    runlog_path: Path,
    *,
    status: str = "partial",
) -> str:
    """Build an inspectable replay script from successful setup commands.

    Only successful mutating `command` events are executable in the output.
    Verification probes and failed commands are kept as comments so the file
    remains useful for audit/debugging without replaying probes blindly.
    """
    events = _load_jsonl(runlog_path)
    run_id = runlog_path.stem
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "# QuickHatch executed setup commands",
        f"# Source run id: {run_id}",
        f"# Source status: {status}",
        "#",
        "# Review before reuse. This script was generated from commands that",
        "# succeeded on one target and is suitable only for matching distro,",
        "# disk layout, hardware, and user choices.",
        "#",
        "# Sensitive values are redacted. Replace ***REDACTED*** placeholders",
        "# before running if a command needs them.",
        "# Verification commands and failed commands are commented out.",
        "",
    ]
    current_stage = ""
    current_group = ""
    emitted = 0
    for event in events:
        event_type = str(event.get("event_type") or "")
        if event_type not in {"command", "verification"}:
            continue
        command = str(event.get("command") or "").strip()
        if not command:
            continue
        stage = str(event.get("stage") or "unknown")
        group = str(event.get("group_id") or "")
        if stage != current_stage:
            lines.extend(["", f"# === {stage} ==="])
            current_stage = stage
            current_group = ""
        if group and group != current_group:
            lines.append(f"# group: {group}")
            current_group = group
        try:
            rc = int(event.get("rc"))
        except (TypeError, ValueError):
            rc = 999
        command = redact_sensitive_text(command)
        if event_type == "command" and rc == 0 and "***REDACTED***" not in command:
            lines.extend([command, ""])
            emitted += 1
        elif event_type == "command" and rc == 0:
            lines.append(_shell_comment(f"EDIT REQUIRED redacted command rc=0: {command}"))
        elif event_type == "verification":
            lines.append(_shell_comment(f"VERIFY rc={rc}: {command}"))
        else:
            lines.append(_shell_comment(f"SKIPPED failed command rc={rc}: {command}"))

    if emitted == 0:
        lines.extend(
            [
                "# No successful mutating setup commands were recorded for replay.",
                "# The setup may have stopped before command execution.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "# End of generated command replay.",
                "# Reboot manually if the original run required it and this script did not.",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def write_executed_commands_script(
    runlog_path: Path,
    *,
    output_dir: Path | None = None,
    status: str = "partial",
) -> Path | None:
    if not runlog_path.exists():
        return None
    target_dir = output_dir or runlog_path.parent
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"quickhatch-executed-commands-{runlog_path.stem}.sh"
        path.write_text(
            build_executed_commands_script(runlog_path, status=status), encoding="utf-8"
        )
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
        return path
    except OSError:
        return None
