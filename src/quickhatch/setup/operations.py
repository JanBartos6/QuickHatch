"""Typed setup operation intent.

This module intentionally does not render distro-specific shell. Family
installers turn validated intent into commands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SUPPORTED_OPERATION_KINDS = frozenset(
    {
        "partition_disk",
        "format_partition",
        "mount_partition",
        "write_fstab",
        "install_bootloader",
        "create_user",
        "enable_network_reconnect",
        "install_ssh_key",
        "controlled_reboot",
        "network_security_change",
        "package_db_repair",
    }
)

DANGEROUS_OPERATION_CAPABILITIES = {
    "partition_disk": "disk_partition",
    "format_partition": "format_filesystem",
    "mount_partition": "mount_target",
    "write_fstab": "write_fstab",
    "install_bootloader": "bootloader_install",
    "controlled_reboot": "controlled_reboot",
    "network_security_change": "network_security_change",
    "package_db_repair": "package_db_repair",
}


@dataclass
class SetupOperation:
    kind: str
    target: str = ""
    filesystem: str = ""
    mountpoint: str = ""
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def capability_kind(self) -> str:
        return DANGEROUS_OPERATION_CAPABILITIES.get(self.kind, "")

    @property
    def erases_data(self) -> bool:
        return self.kind in {"partition_disk", "format_partition", "package_db_repair"}


def parse_setup_operation(raw: Any, *, field_name: str = "operation") -> SetupOperation:
    if not isinstance(raw, dict):
        raise ValueError(f"{field_name} must be an object.")
    kind = str(raw.get("kind", "") or "").strip()
    if kind not in SUPPORTED_OPERATION_KINDS:
        raise ValueError(f"{field_name}.kind is unsupported: {kind or '<empty>'}.")
    options = raw.get("options", {}) or {}
    if not isinstance(options, dict):
        raise ValueError(f"{field_name}.options must be an object.")
    op = SetupOperation(
        kind=kind,
        target=str(raw.get("target", "") or "").strip(),
        filesystem=str(raw.get("filesystem", "") or "").strip(),
        mountpoint=str(raw.get("mountpoint", "") or "").strip(),
        options=dict(options),
    )
    if op.kind in {"partition_disk", "format_partition"} and not op.target:
        raise ValueError(f"{field_name}.target is required for {op.kind}.")
    if op.kind == "format_partition" and not op.filesystem:
        raise ValueError(f"{field_name}.filesystem is required for format_partition.")
    if op.kind == "mount_partition" and (not op.target or not op.mountpoint):
        raise ValueError(f"{field_name}.target and mountpoint are required for mount_partition.")
    return op


def operation_to_payload(operation: SetupOperation) -> dict[str, Any]:
    return {
        "kind": operation.kind,
        "target": operation.target,
        "filesystem": operation.filesystem,
        "mountpoint": operation.mountpoint,
        "options": dict(operation.options),
        "capability_kind": operation.capability_kind,
        "erases_data": operation.erases_data,
    }
