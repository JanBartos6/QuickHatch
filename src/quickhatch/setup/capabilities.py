"""Per-run capabilities for high-blast-radius setup operations."""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from .operations import SetupOperation, operation_to_payload

CAPABILITY_KINDS = frozenset(
    {
        "disk_partition",
        "format_filesystem",
        "mount_target",
        "write_fstab",
        "bootloader_install",
        "controlled_reboot",
        "network_security_change",
        "package_db_repair",
    }
)

DISK_INVENTORY_COMMAND = "lsblk -o NAME,SIZE,MODEL,SERIAL,TYPE,FSTYPE,FSVER,LABEL,UUID,MOUNTPOINTS"

_DEVICE_RE = r"/dev/(?:disk/by-id/[^\s;|&\"']+|(?:sd|vd|xvd|hd)[a-z]\d*|nvme\d+n\d+(?:p\d+)?|mmcblk\d+(?:p\d+)?)"
_TARGET_ROOT_RE = r"(?:/mnt|/target|/mnt/target|/mnt/sysroot|/mnt/sysimage)(?:/\S*|\b|\*)"
_TARGET_ROOT_ALIASES = {
    "/mnt": "/target-root",
    "/target": "/target-root",
    "/mnt/target": "/target-root",
    "/mnt/sysroot": "/target-root",
    "/mnt/sysimage": "/target-root",
}
_WINDOWS_FS_RE = re.compile(r"\b(?:ntfs|bitlocker|windows\s+recovery|msftres)\b", re.I)


@dataclass
class DangerousOperationPreview:
    preview_id: str
    operation: dict[str, Any]
    rendered_commands: list[str]
    install_profile: str
    confirmation_token: str
    one_time_use: bool
    erases_data: bool
    disk_inventory: str = ""


@dataclass
class CapabilityGrant:
    id: str
    kind: str
    target: str = ""
    install_profile: str = "auto"
    confirmation_text: str = ""
    confirmation_id: str = ""
    one_time_use: bool = True
    source: str = "harness"
    reason: str = ""
    timestamp: float = field(default_factory=time.time)
    run_id: str = ""
    consumed: bool = False
    expires_at: float = 0.0
    disk_inventory_hash: str = ""
    preview_id: str = ""


def disk_inventory_hash(inventory: str) -> str:
    return hashlib.sha256(str(inventory or "").encode("utf-8")).hexdigest()


def canonicalize_target(target: str, *, inventory: str = "") -> str:
    """Normalize device/path targets without pretending symlinks are interchangeable.

    If inventory includes a stable by-id string, callers should grant that
    exact target; otherwise this normalizes common Linux device/path spelling
    and target-root aliases.
    """

    text = str(target or "").strip().strip("\"'")
    if not text:
        return ""
    text = re.sub(r"/+", "/", text)
    if text in _TARGET_ROOT_ALIASES:
        return _TARGET_ROOT_ALIASES[text]
    if text.startswith("/dev/disk/by-id/"):
        return text
    if text.startswith("/dev/"):
        return text
    if text in {"fstab", "/etc/fstab", "/mnt/etc/fstab", "/target/etc/fstab"}:
        return "/etc/fstab" if text == "/etc/fstab" else "/target-root/etc/fstab"
    if inventory and text in inventory:
        return text
    return text


def confirmation_token_for(operation: SetupOperation, canonical_target: str) -> str:
    if operation.kind == "partition_disk":
        return f"ERASE {canonical_target}"
    if operation.kind == "format_partition":
        return f"FORMAT {canonical_target}"
    return f"CONFIRM {operation.kind} {canonical_target or operation.mountpoint}".strip()


def build_operation_preview(
    *,
    operation: SetupOperation,
    rendered_commands: list[str],
    install_profile: str,
    canonical_target: str,
    disk_inventory: str = "",
    one_time_use: bool = True,
) -> DangerousOperationPreview:
    return DangerousOperationPreview(
        preview_id=f"preview-{uuid.uuid4().hex[:12]}",
        operation=operation_to_payload(operation),
        rendered_commands=list(rendered_commands),
        install_profile=install_profile,
        confirmation_token=confirmation_token_for(operation, canonical_target),
        one_time_use=one_time_use,
        erases_data=operation.erases_data,
        disk_inventory=disk_inventory,
    )


class CapabilityStore:
    """Run-scoped capability grants with conservative resume semantics."""

    def __init__(self, *, run_id: str, install_profile: str) -> None:
        self.run_id = run_id
        self.install_profile = install_profile or "auto"
        self._grants: dict[str, CapabilityGrant] = {}

    def grant(
        self,
        *,
        kind: str,
        target: str = "",
        confirmation_text: str,
        confirmation_id: str,
        reason: str,
        source: str = "user_confirmed",
        one_time_use: bool = True,
        ttl_seconds: float = 7200,
        disk_inventory: str = "",
        preview_id: str,
    ) -> CapabilityGrant:
        if kind not in CAPABILITY_KINDS:
            raise ValueError(f"Unsupported capability kind: {kind}")
        if not preview_id:
            raise ValueError("Dangerous capabilities require a dry-run preview id.")
        grant = CapabilityGrant(
            id=f"cap-{uuid.uuid4().hex[:12]}",
            kind=kind,
            target=canonicalize_target(target, inventory=disk_inventory),
            install_profile=self.install_profile,
            confirmation_text=confirmation_text,
            confirmation_id=confirmation_id,
            one_time_use=one_time_use,
            source=source,
            reason=reason,
            run_id=self.run_id,
            expires_at=time.time() + ttl_seconds if ttl_seconds else 0.0,
            disk_inventory_hash=disk_inventory_hash(disk_inventory) if disk_inventory else "",
            preview_id=preview_id,
        )
        self._grants[grant.id] = grant
        return grant

    def allows(
        self,
        *,
        kind: str,
        target: str = "",
        disk_inventory: str = "",
    ) -> CapabilityGrant | None:
        canonical = canonicalize_target(target, inventory=disk_inventory)
        now = time.time()
        current_inventory_hash = disk_inventory_hash(disk_inventory) if disk_inventory else ""
        for grant in self._grants.values():
            if grant.kind != kind or grant.run_id != self.run_id:
                continue
            if grant.install_profile != self.install_profile:
                continue
            if grant.consumed:
                continue
            if grant.expires_at and grant.expires_at < now:
                continue
            if grant.target and not canonical:
                continue
            if grant.target and canonical and grant.target != canonical:
                continue
            if grant.disk_inventory_hash:
                if (
                    not current_inventory_hash
                    or grant.disk_inventory_hash != current_inventory_hash
                ):
                    continue
            return grant
        return None

    def consume(self, capability_id: str) -> bool:
        grant = self._grants.get(capability_id)
        if not grant:
            return False
        if grant.one_time_use:
            grant.consumed = True
        return True

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "install_profile": self.install_profile,
            "grants": [asdict(grant) for grant in self._grants.values()],
        }

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        run_id: str,
        install_profile: str,
    ) -> CapabilityStore:
        store = cls(run_id=run_id, install_profile=install_profile)
        if not isinstance(payload, dict):
            return store
        if payload.get("run_id") != run_id or payload.get("install_profile") != install_profile:
            return store
        for raw in payload.get("grants", []) or []:
            if not isinstance(raw, dict):
                continue
            try:
                grant = CapabilityGrant(
                    **{k: v for k, v in raw.items() if k in CapabilityGrant.__dataclass_fields__}
                )
            except TypeError:
                continue
            store._grants[grant.id] = grant
        return store


def _shell_payloads(command: str) -> list[str]:
    payloads: list[str] = []
    for match in re.finditer(
        r"(?:sudo\s+)?(?:bash|sh|zsh|fish)\s+-c\s+(['\"])(?P<payload>.*?)(?<!\\)\1",
        command,
        re.I | re.S,
    ):
        payloads.append(match.group("payload"))
    return payloads


def dangerous_command_requirements(command: str) -> list[dict[str, str]]:
    """Return capability requirements for dangerous shell snippets."""

    requirements: list[dict[str, str]] = []
    text = str(command or "")
    scan = [text, *_shell_payloads(text)]
    for item in scan:
        lowered = item.lower()
        for match in re.finditer(
            r"\bmkfs(?:\.[\w+-]+)?\b(?:[^;&|]*)\s(?P<target>" + _DEVICE_RE + r")", item, re.I
        ):
            requirements.append(
                {
                    "kind": "format_filesystem",
                    "target": match.group("target"),
                    "reason": "filesystem format command",
                }
            )
        for match in re.finditer(
            r"\bcryptsetup\b[^;&|]*(?:luksformat|luksFormat)\b[^;&|]*(?P<target>"
            + _DEVICE_RE
            + r")",
            item,
            re.I,
        ):
            requirements.append(
                {
                    "kind": "format_filesystem",
                    "target": match.group("target"),
                    "reason": "LUKS format command",
                }
            )
        for match in re.finditer(
            r"\b(?:parted|sgdisk|sfdisk|fdisk)\b[^;&|]*(?:mklabel|mkpart|zap|delete|--zap-all|-Z|-o)\b[^;&|]*(?P<target>"
            + _DEVICE_RE
            + r")?",
            item,
            re.I,
        ):
            target = match.groupdict().get("target") or _first_device(item)
            requirements.append(
                {
                    "kind": "disk_partition",
                    "target": target,
                    "reason": "partition table mutation command",
                }
            )
        for match in re.finditer(r"\bwipefs\b[^;&|]*(?P<target>" + _DEVICE_RE + r")", item, re.I):
            requirements.append(
                {
                    "kind": "disk_partition",
                    "target": match.group("target"),
                    "reason": "wipefs disk signature mutation",
                }
            )
        for tool in ("blkdiscard", "pvcreate"):
            for match in re.finditer(
                rf"\b{tool}\b[^;&|]*(?P<target>{_DEVICE_RE})",
                item,
                re.I,
            ):
                requirements.append(
                    {
                        "kind": "disk_partition",
                        "target": match.group("target"),
                        "reason": f"{tool} disk metadata mutation",
                    }
                )
        for match in re.finditer(
            r"\bdd\b[^;&|]*\bof=(?P<target>" + _DEVICE_RE + r")",
            item,
            re.I,
        ):
            requirements.append(
                {
                    "kind": "disk_partition",
                    "target": match.group("target"),
                    "reason": "raw block-device write",
                }
            )
        for match in re.finditer(
            r"\bmount\b(?!point\b)[^;&|]*(?P<source>"
            + _DEVICE_RE
            + r")[^;&|]*(?P<target>"
            + _TARGET_ROOT_RE
            + r")",
            item,
            re.I,
        ):
            requirements.append(
                {
                    "kind": "mount_target",
                    "target": match.group("target"),
                    "reason": "target-root mount operation",
                }
            )
        for match in re.finditer(
            r"\bumount\b[^;&|]*(?P<target>" + _TARGET_ROOT_RE + r")",
            item,
            re.I,
        ):
            requirements.append(
                {
                    "kind": "mount_target",
                    "target": match.group("target"),
                    "reason": "target-root unmount operation",
                }
            )
        if re.search(r"\b(?:grub-install|grub2-install|bootctl\s+install)\b", item, re.I):
            requirements.append(
                {"kind": "bootloader_install", "target": "/boot", "reason": "bootloader install"}
            )
        if re.search(r"\befibootmgr\b[^\n;|&]*(?:\s-c\b|--create\b)", item, re.I):
            requirements.append(
                {
                    "kind": "bootloader_install",
                    "target": _first_device(item) or "/boot",
                    "reason": "EFI boot entry creation",
                }
            )
        if re.search(
            r"\b(?:genfstab\b|/etc/fstab\b|/mnt/etc/fstab\b|/target/etc/fstab\b)", item, re.I
        ) and re.search(r">\s*/|tee\s+", item):
            requirements.append(
                {"kind": "write_fstab", "target": "/target-root/etc/fstab", "reason": "fstab write"}
            )
        if re.search(r"\b(?:systemctl\s+)?reboot\b|\bshutdown\s+-r\b|\binit\s+6\b", item, re.I):
            requirements.append(
                {"kind": "controlled_reboot", "target": "", "reason": "controlled reboot"}
            )
        if re.search(
            r"\brm\b[^\n;|&]*(?:-[A-Za-z]*r[A-Za-z]*f|-[A-Za-z]*f[A-Za-z]*r)[^\n;|&]*"
            + _TARGET_ROOT_RE,
            item,
            re.I,
        ):
            requirements.append(
                {
                    "kind": "disk_partition",
                    "target": "/target-root",
                    "reason": "recursive destructive remove under target root",
                }
            )
        firewall_mutates = (
            re.search(
                r"\b(?:ufw\s+enable|systemctl\s+(?:enable|start|restart|--now\s+enable).*(?:mullvad|wg-quick|firewalld|nftables)|wg-quick\s+up)\b",
                item,
                re.I,
            )
            or re.search(r"\bnft\s+(?:add|delete|destroy|flush|insert|replace)\b", item, re.I)
            or re.search(
                r"\biptables\b[^\n;|&]*(?:\s-[ADIRFPNXZ]\b|--(?:append|delete|insert|replace|flush|policy|delete-chain|new-chain|zero)\b)",
                item,
            )
            or re.search(
                r"\bfirewall-cmd\b[^\n;|&]*(?:--reload|--complete-reload|--add-|--remove-|--set-default)",
                item,
                re.I,
            )
        )
        if firewall_mutates:
            requirements.append(
                {
                    "kind": "network_security_change",
                    "target": "",
                    "reason": "network security activation/mutation",
                }
            )
        if re.search(
            r"(?:\brpm\s+--rebuilddb\b|\bdpkg\s+--configure\b|(?:^|\s)-rdd(?:\s|$)|--nodeps\b|\brm\s+-f\s+/var/lib/(?:pacman|dpkg|rpm))",
            lowered,
        ):
            requirements.append(
                {
                    "kind": "package_db_repair",
                    "target": "/var/lib",
                    "reason": "package database repair",
                }
            )
    return _dedupe_requirements(requirements)


def _first_device(text: str) -> str:
    match = re.search(_DEVICE_RE, text or "", re.I)
    return match.group(0) if match else ""


def _dedupe_requirements(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, str]] = []
    for item in items:
        key = (item.get("kind", ""), canonicalize_target(item.get("target", "")))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def dual_boot_blocks_operation(operation: SetupOperation, disk_inventory: str) -> str:
    if operation.kind not in {"partition_disk", "format_partition"}:
        return ""
    if _WINDOWS_FS_RE.search(disk_inventory or ""):
        return (
            "dual_boot_free_space cannot shrink, delete, or format NTFS/BitLocker/"
            "Windows recovery/existing EFI partitions automatically."
        )
    return ""
