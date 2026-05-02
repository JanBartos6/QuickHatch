"""Failure classification for setup replay and repair routing."""

from __future__ import annotations

import re

from .types import FailureCategory, FailureClassification

_PATTERNS: tuple[tuple[FailureCategory, tuple[str, ...]], ...] = (
    (
        FailureCategory.NETWORK,
        (
            "could not resolve",
            "temporary failure resolving",
            "name or service not known",
            "network is unreachable",
            "connection timed out",
            "remote end closed connection",
            "failed retrieving file",
            "failed to download",
        ),
    ),
    (
        FailureCategory.PACKAGE_MISSING,
        (
            "target not found",
            "unable to locate package",
            "no match for argument",
            "no package matches",
            "package not found",
        ),
    ),
    (
        FailureCategory.PACKAGE_CONFLICT,
        (
            "conflicting dependencies",
            "unresolvable package conflicts",
            "breaks existing package",
            "file exists in filesystem",
        ),
    ),
    (
        FailureCategory.SUDO_PASSWORD,
        (
            "sudo: a terminal is required",
            "sudo: a password is required",
            "incorrect password",
            "authentication failure",
        ),
    ),
    (
        FailureCategory.BOOTLOADER,
        (
            "grub-install",
            "grub2-install",
            "bootloader",
            "efibootmgr",
            "no bootloader installed",
        ),
    ),
    (
        FailureCategory.FSTAB,
        ("fstab", "genfstab", "findmnt", "mount point does not exist"),
    ),
    (
        FailureCategory.SSH_RECONNECT,
        (
            "permission denied (publickey",
            "connection refused",
            "no route to host",
            "ssh reconnect",
            "could not resolve hostname",
        ),
    ),
    (
        FailureCategory.DISPLAY_SESSION,
        (
            "no gui session",
            "cannot connect to display",
            "kscreen",
            "plasma-apply",
            "dbus",
        ),
    ),
    (
        FailureCategory.DRIVER_PENDING_REBOOT,
        (
            "module nvidia_drm not found",
            "nvidia-smi has failed",
            "driver/library version mismatch",
            "/sys/module/nvidia",
            "needs reboot",
        ),
    ),
    (FailureCategory.TIMEOUT, ("timed out", "timeout", "read timeout")),
    (
        FailureCategory.MODEL_JSON,
        ("invalid json", "unterminated", "research plan validation failed", "planner returned"),
    ),
)


def classify_failure(
    *,
    command: str = "",
    rc: int | None = None,
    stdout: str = "",
    stderr: str = "",
    detail: str = "",
) -> FailureClassification:
    """Classify a command/model/setup failure into stable replay categories."""

    haystack = "\n".join(str(part or "") for part in (command, stdout, stderr, detail)).lower()
    if rc in {124, 408}:
        return FailureClassification(
            category=FailureCategory.TIMEOUT,
            detail="Command or model call timed out.",
            confidence="high",
        )
    for category, needles in _PATTERNS:
        if any(needle in haystack for needle in needles):
            return FailureClassification(category=category, detail=detail or stderr or stdout)
    if re.search(r"\b(rc|exit status)\s*=\s*1\b", haystack):
        return FailureClassification(
            category=FailureCategory.UNKNOWN,
            detail=detail or stderr or stdout,
            confidence="low",
        )
    return FailureClassification(
        category=FailureCategory.UNKNOWN,
        detail=detail or stderr or stdout,
        confidence="low",
    )
