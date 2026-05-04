"""Repair-attempt signatures for loop detection without blocking progress."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from .failure import classify_failure


@dataclass(frozen=True)
class AttemptSignature:
    stage: str
    group_id: str
    group_title: str
    failure_category: str
    operation: str
    tool: str
    packages: tuple[str, ...] = ()
    target: str = ""
    strategy: str = ""

    @property
    def loop_key(self) -> tuple[str, str, str, tuple[str, ...], str, str]:
        return (
            self.failure_category,
            self.operation,
            self.tool,
            self.packages,
            self.target,
            self.strategy,
        )


def build_attempt_signature(
    *,
    stage: str,
    group_id: str,
    group_title: str,
    command: str,
    stdout: str = "",
    stderr: str = "",
    detail: str = "",
    strategy: str = "",
) -> AttemptSignature:
    classification = classify_failure(command=command, stdout=stdout, stderr=stderr, detail=detail)
    tokens = _tokens(command)
    tool = _tool(tokens)
    packages = tuple(sorted(_packages(tool, tokens)))
    target = _target(command)
    return AttemptSignature(
        stage=stage,
        group_id=group_id,
        group_title=group_title,
        failure_category=classification.category.value,
        operation=_operation(tool, command),
        tool=tool,
        packages=packages,
        target=target,
        strategy=strategy or _strategy(command),
    )


def is_diagnostic_command(command: str) -> bool:
    text = str(command or "").strip().lower()
    return bool(
        re.match(
            r"^(?:sudo\s+)?(?:pacman\s+-s|apt(?:-cache)?\s+search|dnf\s+search|"
            r"yay\s+-ss|lsblk|blkid|findmnt|mountpoint|systemctl\s+status|"
            r"journalctl|cat\s+|grep\s+|test\s+|command\s+-v|which\s+)",
            text,
        )
    )


def changed_strategy_allowed(previous: AttemptSignature, current: AttemptSignature) -> bool:
    if previous.failure_category != current.failure_category:
        return True
    if previous.operation != current.operation:
        return True
    if previous.tool != current.tool:
        return True
    if previous.strategy != current.strategy:
        return True
    if previous.packages != current.packages:
        return True
    return previous.target != current.target


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command or "")
    except ValueError:
        return str(command or "").split()


def _tool(tokens: list[str]) -> str:
    stripped = [token for token in tokens if token not in {"sudo", "env"} and "=" not in token[:3]]
    if not stripped:
        return ""
    if stripped[0] in {"bash", "sh", "zsh", "fish"} and "-c" in stripped:
        return "shell"
    return stripped[0].rsplit("/", 1)[-1].lower()


def _operation(tool: str, command: str) -> str:
    text = str(command or "").lower()
    if tool in {
        "pacman",
        "apt",
        "apt-get",
        "dnf",
        "zypper",
        "apk",
        "xbps-install",
        "emerge",
        "yay",
        "paru",
        "flatpak",
    }:
        return "package_install" if re.search(r"\b(?:install|add|-s|--sync)\b", text) else "package"
    if "mkfs" in tool or "mkfs." in text:
        return "format_filesystem"
    if tool in {"parted", "sgdisk", "sfdisk", "fdisk", "wipefs"}:
        return "disk_partition"
    if "grub-install" in text or "bootctl install" in text:
        return "bootloader_install"
    if "fstab" in text:
        return "write_fstab"
    return tool or "unknown"


def _strategy(command: str) -> str:
    text = str(command or "").lower()
    if "flatpak" in text:
        return "flatpak"
    if re.search(r"\b(?:yay|paru)\b", text):
        return "community"
    if re.search(r"\b(?:pacman|apt|apt-get|dnf|zypper|apk|xbps-install|emerge)\b", text):
        return "repo"
    if re.search(r"\b(?:search|-ss|-s)\b", text):
        return "search"
    return "local"


def _packages(tool: str, tokens: list[str]) -> list[str]:
    if tool not in {
        "pacman",
        "apt",
        "apt-get",
        "dnf",
        "zypper",
        "apk",
        "xbps-install",
        "emerge",
        "yay",
        "paru",
        "flatpak",
    }:
        return []
    result: list[str] = []
    skip_next = False
    for token in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if token in {
            "install",
            "add",
            "-S",
            "-Sy",
            "-Syu",
            "-Syyu",
            "--needed",
            "-y",
            "--noconfirm",
        }:
            continue
        if token.startswith("-"):
            if token in {"-o", "--root", "--installroot", "--releasever"}:
                skip_next = True
            continue
        if "=" in token and token.split("=", 1)[0].startswith("-"):
            continue
        result.append(token)
    return result


def _target(command: str) -> str:
    match = re.search(
        r"/dev/(?:disk/by-id/[^\s;|&\"']+|(?:sd|vd|xvd|hd)[a-z]\d*|nvme\d+n\d+(?:p\d+)?|mmcblk\d+(?:p\d+)?)",
        command or "",
    )
    if match:
        return match.group(0)
    if "/mnt" in (command or ""):
        return "/mnt"
    return ""
