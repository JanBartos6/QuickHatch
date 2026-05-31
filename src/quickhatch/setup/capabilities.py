"""Per-run capabilities for high-blast-radius setup operations."""

from __future__ import annotations

import hashlib
import re
import shlex
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
        "bootloader_config",
        "controlled_reboot",
        "network_security_change",
        "package_db_repair",
        "filesystem_repair",
        "filesystem_resize",
        "storage_topology_change",
        "initramfs_rebuild",
        "boot_artifact_update",
    }
)

DISK_INVENTORY_COMMAND = "lsblk -o NAME,SIZE,MODEL,SERIAL,TYPE,FSTYPE,FSVER,LABEL,UUID,MOUNTPOINTS"

_DEVICE_RE = (
    r"/dev/(?:disk/by-(?:id|uuid|label|partuuid|path)/[^\s;|&\"']+|"
    r"mapper/[^\s;|&\"']+|[A-Za-z0-9_.+-]+/[^\s;|&\"']+|"
    r"(?:sd|vd|xvd|hd)[a-z]\d*|nvme\d+n\d+(?:p\d+)?|mmcblk\d+(?:p\d+)?)"
)
_TARGET_ROOT_RE = (
    r"(?:/target-root|/mnt/target|/mnt/sysroot|/mnt/sysimage|/target|/mnt)(?:/\S*|\b|\*)"
)
_INSTALL_MOUNTPOINT_RE = (
    r"(?:/target-root|/mnt/target|/mnt/sysroot|/mnt/sysimage|/mnt/boot/efi|"
    r"/mnt/boot|/mnt/efi|/target|/mnt)(?:\b|$)"
)
_TARGET_ROOT_ALIASES = {
    "/target-root": "/target-root",
    "/mnt": "/target-root",
    "/target": "/target-root",
    "/mnt/target": "/target-root",
    "/mnt/sysroot": "/target-root",
    "/mnt/sysimage": "/target-root",
}
_WINDOWS_FS_RE = re.compile(r"\b(?:ntfs|bitlocker|windows\s+recovery|msftres)\b", re.I)
_FSTAB_PATH_RE = (
    r"(?:/(?:target-root|mnt|target|mnt/target|mnt/sysroot|mnt/sysimage)/etc/fstab|/etc/fstab)\b"
)
_PACKAGE_DB_PATH_RE = r"/var/lib/(?:dpkg|pacman|rpm)(?:/[^\s;|&\"']*)?"


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
    if "->" in text:
        source, destination = (part.strip() for part in text.split("->", 1))
        return (
            f"{canonicalize_target(source, inventory=inventory)} -> "
            f"{canonicalize_target(destination, inventory=inventory)}"
        )
    text = re.sub(r"/+", "/", text)
    if text in _TARGET_ROOT_ALIASES:
        return _TARGET_ROOT_ALIASES[text]
    if text.startswith("/dev/disk/by-id/"):
        return text
    if text.startswith("/dev/"):
        return text
    if text in {
        "fstab",
        "/etc/fstab",
        "/mnt/etc/fstab",
        "/target/etc/fstab",
        "/target-root/etc/fstab",
    }:
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
            if grant.target != canonical:
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
        segment_prefix = re.split(r"(?:[;&|]|\n)+", command[: match.start()])[-1].strip()
        try:
            prefix_tokens = shlex.split(segment_prefix, posix=True)
        except ValueError:
            prefix_tokens = segment_prefix.split()
        stripped_prefix = _strip_command_prefix(prefix_tokens)
        if stripped_prefix and stripped_prefix[0].rsplit("/", 1)[-1].lower() in {
            "arch-chroot",
            "chroot",
        }:
            continue
        payloads.append(match.group("payload"))
    return payloads


_SIMPLE_ASSIGN_RE = re.compile(
    r"(?<![\w$])(?P<name>[A-Za-z_][A-Za-z0-9_]*)="
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s;&|]+)"
)
_VAR_REF_RE = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))"
)


def _strip_assignment_quotes(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _expand_var_refs(text: str, values: dict[str, str]) -> str:
    expanded = str(text or "")
    for _ in range(4):
        changed = False

        def replace(match: re.Match[str]) -> str:
            nonlocal changed
            name = match.group("braced") or match.group("plain") or ""
            if name in values:
                changed = True
                return values[name]
            return match.group(0)

        next_text = _VAR_REF_RE.sub(replace, expanded)
        expanded = next_text
        if not changed:
            break
    return expanded


def _value_references_assignment_name(value: str, name: str) -> bool:
    return bool(re.search(rf"\$(?:{{{re.escape(name)}(?:[}}:]|$)|{re.escape(name)}\b)", value))


def _expand_static_command_substitutions(text: str) -> str:
    """Expand tiny deterministic command substitutions used in shell wrappers."""

    pattern = re.compile(
        r"\$\(\s*printf\s+(?:%s\s+)?(?P<quote>['\"]?)(?P<value>[A-Za-z0-9_.+-]+)(?P=quote)\s*\)"
    )
    return pattern.sub(lambda match: match.group("value"), str(text or ""))


def _expand_simple_shell_variables(text: str) -> str:
    """Resolve simple shell assignments before high-blast-radius scanning.

    This intentionally supports only deterministic local assignments such as
    ``DISK=/dev/sda; ROOT=${DISK}1``. Anything more dynamic remains visible as
    an unresolved variable and is handled by the ambiguous high-blast fallback.
    """

    source = str(text or "")
    values: dict[str, str] = {}
    for _ in range(4):
        changed = False
        for match in _SIMPLE_ASSIGN_RE.finditer(source):
            name = match.group("name")
            value = _strip_assignment_quotes(match.group("value"))
            if _value_references_assignment_name(value, name):
                continue
            value = _expand_var_refs(value, values)
            if values.get(name) != value:
                values[name] = value
                changed = True
        if not changed:
            break
    if not values:
        return source
    return _expand_var_refs(source, values)


def _partition_paths_for_disk(disk: str) -> tuple[str, str]:
    suffix = "p" if str(disk or "").endswith(tuple("0123456789")) else ""
    return f"{disk}{suffix}1", f"{disk}{suffix}2"


def _expand_quickhatch_target_disk_variables(text: str) -> str:
    """Resolve the deterministic install-spine partition variables."""

    source = str(text or "")
    match = re.search(
        r"\bQUICKHATCH_TARGET_DISK=(?P<value>\"[^\"]*\"|'[^']*'|[^\s;&|]+)",
        source,
    )
    if not match:
        return source
    disk = _strip_assignment_quotes(match.group("value"))
    if not re.fullmatch(_DEVICE_RE, disk, re.I):
        return source
    esp, root = _partition_paths_for_disk(disk)
    values = {
        "QUICKHATCH_TARGET_DISK": disk,
        "DISK": disk,
        "ESP": esp,
        "ROOT": root,
        "ESP_PARTITION": esp,
        "ROOT_PARTITION": root,
    }
    expanded = source
    for name, value in values.items():
        expanded = re.sub(rf"\$\{{{re.escape(name)}:[^}}]*\}}", value, expanded)
        expanded = re.sub(rf"\$\{{{re.escape(name)}\}}", value, expanded)
        expanded = re.sub(rf"\${re.escape(name)}\b", value, expanded)
    return expanded


def _expanded_shell_scans(command: str) -> list[str]:
    scans: list[str] = []
    seen: set[str] = set()

    def add_scan(item: str, *, include_payloads: bool = True) -> None:
        expanded = _expand_static_command_substitutions(
            _expand_simple_shell_variables(_expand_quickhatch_target_disk_variables(item))
        )
        if expanded and expanded not in seen:
            scans.append(expanded)
            seen.add(expanded)
        if include_payloads:
            for payload in _shell_payloads(expanded):
                add_scan(payload, include_payloads=False)

    add_scan(str(command or ""))
    for item in _shell_payloads(str(command or "")):
        add_scan(item)
    return scans


def _command_segments(text: str) -> list[str]:
    return [
        segment.strip() for segment in re.split(r"(?:[;&|]|\n)+", text or "") if segment.strip()
    ]


def _strip_shell_comments(text: str) -> str:
    result: list[str] = []
    quote: str | None = None
    escaped = False
    idx = 0
    source = str(text or "")
    while idx < len(source):
        char = source[idx]
        if escaped:
            result.append(char)
            escaped = False
            idx += 1
            continue
        if char == "\\":
            result.append(char)
            escaped = True
            idx += 1
            continue
        if quote:
            result.append(char)
            if char == quote:
                quote = None
            idx += 1
            continue
        if char in {"'", '"'}:
            quote = char
            result.append(char)
            idx += 1
            continue
        if char == "#" and (idx == 0 or source[idx - 1].isspace()):
            while idx < len(source) and source[idx] != "\n":
                idx += 1
            continue
        result.append(char)
        idx += 1
    return "".join(result)


def _printed_text_is_piped_to_shell(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:echo|printf|cat)\b[^\n;|&]*\|\s*(?:sudo\s+)?(?:bash|sh|zsh|fish)\b",
            text or "",
            re.I,
        )
    )


def _segment_tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _all_shell_assignments(tokens: list[str]) -> bool:
    return bool(tokens) and all(
        "=" in token and not token.startswith("-") and token.split("=", 1)[0].isidentifier()
        for token in tokens
    )


def _assignment_segment_carries_device_target(segment: str) -> bool:
    if not re.search(_DEVICE_RE, segment or "", re.I):
        return False
    return not re.search(
        r"\b(?:mkfs(?:\.[\w+-]+)?|mkswap|cryptsetup|parted|sgdisk|sfdisk|fdisk|wipefs|dd|"
        r"blkdiscard|pvcreate|pvremove|vgcreate|vgremove|lvcreate|lvremove|shred)\b",
        segment,
        re.I,
    )


def _shell_c_payload(tokens: list[str]) -> str:
    for idx, token in enumerate(tokens[:-1]):
        if token == "-c":
            return tokens[idx + 1]
    return ""


def _python_c_payload(tokens: list[str]) -> str:
    for idx, token in enumerate(tokens[:-1]):
        if token == "-c":
            return tokens[idx + 1]
    return ""


def _python_payload_executes_shell(payload: str) -> bool:
    return bool(
        re.search(
            r"\b(?:os\.system|subprocess\.(?:run|call|Popen|check_call|check_output)|system)\s*\(",
            payload or "",
        )
    )


def _python_payload_prints_only(payload: str) -> bool:
    stripped = str(payload or "").strip()
    return bool(
        stripped.startswith("print(")
        and not re.search(r"\b(?:open|write|unlink|remove|rename|replace)\s*\(", stripped)
        and not _python_payload_executes_shell(stripped)
    )


def _executable_shell_scan_text(text: str) -> str:
    """Keep executable shell text while dropping inert printed/commented strings."""

    uncommented = _strip_shell_comments(text)
    if _printed_text_is_piped_to_shell(uncommented):
        return uncommented
    executable_segments: list[str] = []
    for segment in _command_segments(uncommented):
        tokens = _segment_tokens(segment)
        if _all_shell_assignments(tokens):
            if _assignment_segment_carries_device_target(segment):
                executable_segments.append(segment)
            continue
        stripped = _strip_command_prefix(tokens)
        if not stripped:
            continue
        tool = stripped[0].rsplit("/", 1)[-1].lower()
        if tool in {"echo", "printf"}:
            if "$(" in segment:
                executable_segments.append(segment)
            continue
        if tool in {"bash", "sh", "zsh", "fish"}:
            payload = _shell_c_payload(stripped)
            executable_segments.append(payload if payload else segment)
            continue
        if tool in {"python", "python3"}:
            payload = _python_c_payload(stripped)
            if payload and _python_payload_prints_only(payload):
                executable_segments.append(f"{tool} -c")
                continue
        executable_segments.append(segment)
    return "; ".join(executable_segments)


def _strip_command_prefix(tokens: list[str]) -> list[str]:
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        if "=" in token and not token.startswith("-") and token.split("=", 1)[0].isidentifier():
            idx += 1
            continue
        if token == "env":
            idx += 1
            while (
                idx < len(tokens)
                and "=" in tokens[idx]
                and not tokens[idx].startswith("-")
                and tokens[idx].split("=", 1)[0].isidentifier()
            ):
                idx += 1
            continue
        if token == "sudo":
            idx += 1
            while idx < len(tokens) and tokens[idx].startswith("-"):
                option = tokens[idx]
                idx += 1
                if option in {
                    "-C",
                    "--close-from",
                    "-D",
                    "--chdir",
                    "-g",
                    "--group",
                    "-h",
                    "--host",
                    "-p",
                    "--prompt",
                    "-T",
                    "--command-timeout",
                    "-u",
                    "--user",
                } and idx < len(tokens):
                    idx += 1
            continue
        break
    return tokens[idx:]


def _stripped_segment_tokens(item: str) -> list[list[str]]:
    segments: list[list[str]] = []
    for segment in _command_segments(item):
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            tokens = segment.split()
        stripped = _strip_command_prefix(tokens)
        if stripped:
            segments.append(stripped)
    return segments


def _non_option_args(args: list[str]) -> list[str]:
    return [arg for arg in args if arg and not arg.startswith("-")]


def _has_any_option(args: list[str], *options: str) -> bool:
    return any(arg in options for arg in args)


def _partition_editor_requirement(tokens: list[str], item: str) -> dict[str, str] | None:
    if not tokens:
        return None
    tool = tokens[0].rsplit("/", 1)[-1].lower()
    args = tokens[1:]
    target = _first_device(item)
    if not target:
        return None
    if tool == "fdisk":
        if _has_any_option(args, "-l", "--list"):
            return None
        return {
            "kind": "disk_partition",
            "target": target,
            "reason": "interactive fdisk partition editor",
        }
    if tool in {"cfdisk", "cgdisk"}:
        return {
            "kind": "disk_partition",
            "target": target,
            "reason": f"interactive {tool} partition editor",
        }
    if tool in {"gdisk", "sgdisk"}:
        if _has_any_option(args, "-l", "--list", "-p", "--print"):
            return None
        if tool == "sgdisk":
            return None
        return {
            "kind": "disk_partition",
            "target": target,
            "reason": "interactive gdisk partition editor",
        }
    if tool == "parted":
        non_options = _non_option_args(args)
        if not non_options or target not in non_options:
            return None
        target_index = non_options.index(target)
        verbs = {arg.lower() for arg in non_options[target_index + 1 :]}
        if verbs and verbs <= {"print", "unit", "help", "version"}:
            return None
        if not verbs:
            return {
                "kind": "disk_partition",
                "target": target,
                "reason": "interactive parted partition editor",
            }
    return None


def _option_value(args: list[str], *names: str) -> str:
    for idx, arg in enumerate(args):
        if arg in names and idx + 1 < len(args):
            return args[idx + 1]
        for name in names:
            prefix = f"{name}="
            if arg.startswith(prefix):
                return arg[len(prefix) :]
    return ""


def _chroot_context(tokens: list[str]) -> tuple[str, list[str]]:
    if not tokens:
        return "", []
    tool = tokens[0].rsplit("/", 1)[-1].lower()
    if tool not in {"arch-chroot", "chroot"} or len(tokens) < 3:
        return "", tokens
    root = canonicalize_target(tokens[1])
    return root, tokens[2:]


def _command_token_contexts(tokens: list[str]) -> list[tuple[str, list[str]]]:
    root, command_tokens = _chroot_context(tokens)
    contexts = [(root, command_tokens)]
    if not command_tokens:
        return contexts
    tool = command_tokens[0].rsplit("/", 1)[-1].lower()
    if tool in {"bash", "sh", "zsh", "fish"}:
        payload = _shell_c_payload(command_tokens)
        if payload:
            contexts.extend(
                (root, payload_tokens) for payload_tokens in _stripped_segment_tokens(payload)
            )
    return contexts


def _join_chroot_path(root: str, path: str) -> str:
    if not root:
        return path
    if not path.startswith("/"):
        return root
    return canonicalize_target(root).rstrip("/") + path


def _strip_path_quotes(path: str) -> str:
    return str(path or "").strip().strip("\"'")


def _filesystem_repair_target(tokens: list[str], item: str) -> str:
    tool = tokens[0].rsplit("/", 1)[-1].lower() if tokens else ""
    args = tokens[1:]
    if tool in {"fsck", "e2fsck"}:
        if any(arg in {"-n", "--no", "-N", "--dry-run"} for arg in args):
            return ""
        if tool == "fsck" and not any(
            arg == "-y" or (arg.startswith("-") and not arg.startswith("--") and "y" in arg[1:])
            for arg in args
        ):
            return ""
        if tool == "e2fsck" and not any(
            arg == "-y"
            or arg == "-p"
            or arg == "-a"
            or (
                arg.startswith("-")
                and not arg.startswith("--")
                and any(flag in arg[1:] for flag in {"f", "y", "p", "a"})
            )
            for arg in args
        ):
            return ""
        return _first_device(item)
    if tool == "xfs_repair":
        if "-n" in args:
            return ""
        return _first_device(item)
    if tool == "btrfs" and len(args) >= 2 and args[0] == "check" and "--repair" in args:
        return _first_device(item)
    return ""


def _filesystem_resize_target(tokens: list[str], item: str) -> str:
    tool = tokens[0].rsplit("/", 1)[-1].lower() if tokens else ""
    args = tokens[1:]
    if tool == "resize2fs":
        return _first_device(item)
    if tool == "xfs_growfs" and args:
        return _strip_path_quotes(args[-1])
    if tool == "btrfs" and len(args) >= 3 and args[:2] == ["filesystem", "resize"]:
        return _strip_path_quotes(args[-1])
    return ""


def _storage_topology_requirement(tokens: list[str], item: str) -> dict[str, str] | None:
    if not tokens:
        return None
    tool = tokens[0].rsplit("/", 1)[-1].lower()
    args = tokens[1:]
    if tool == "btrfs" and len(args) >= 3 and args[:2] == ["device", "add"]:
        return {
            "kind": "storage_topology_change",
            "target": _first_device(item),
            "reason": "btrfs device topology change",
        }
    if (
        tool == "btrfs"
        and len(args) >= 3
        and tuple(args[:2])
        in {
            ("device", "delete"),
            ("device", "remove"),
        }
    ):
        return {
            "kind": "storage_topology_change",
            "target": _first_device(item),
            "reason": "btrfs device topology change",
        }
    if tool == "zpool" and args and args[0] in {"attach", "detach", "add", "remove", "replace"}:
        target = args[1] if len(args) > 1 else ""
        return {
            "kind": "storage_topology_change",
            "target": _strip_path_quotes(target),
            "reason": "ZFS pool topology change",
        }
    return None


def _initramfs_rebuild_target(tokens: list[str], root: str = "") -> str:
    if not tokens:
        return ""
    tool = tokens[0].rsplit("/", 1)[-1].lower()
    args = tokens[1:]
    if tool == "update-initramfs":
        if any(arg in {"-u", "--update", "-c", "--create", "-d", "--delete"} for arg in args):
            return _join_chroot_path(root, "/boot")
        return ""
    if tool == "dracut":
        if any(
            arg in {"--print-cmdline", "--list-modules", "--help", "-h", "--version"}
            for arg in args
        ):
            return ""
        mutates = any(
            arg in {"--force", "-f", "--regenerate-all", "-H", "--hostonly"} for arg in args
        )
        output = next((arg for arg in args if arg.startswith("/boot/")), "")
        if mutates or output:
            return _join_chroot_path(root, "/boot")
        return ""
    if tool == "mkinitcpio":
        if any(arg in {"-P", "--allpresets"} for arg in args):
            return _join_chroot_path(root, "/boot")
        if "-g" in args:
            return _join_chroot_path(root, _option_value(args, "-g"))
        return ""
    return ""


def _boot_artifact_update_target(tokens: list[str], root: str = "") -> str:
    if not tokens:
        return ""
    tool = tokens[0].rsplit("/", 1)[-1].lower()
    args = tokens[1:]
    if tool == "kernel-install" and args and args[0] in {"add", "remove"}:
        return _join_chroot_path(root, "/boot")
    return ""


def _canonical_fstab_target(path: str) -> str:
    cleaned = _strip_path_quotes(path)
    if cleaned == "/etc/fstab":
        return "/etc/fstab"
    if cleaned.endswith("/etc/fstab"):
        return "/target-root/etc/fstab"
    return cleaned


def _path_is_exact_fstab(path: str) -> bool:
    return _canonical_fstab_target(path) in {"/etc/fstab", "/target-root/etc/fstab"}


def _last_non_option_path(tokens: list[str]) -> str:
    candidates = [arg for arg in tokens[1:] if arg and not arg.startswith("-")]
    return candidates[-1] if candidates else ""


def _copy_like_writes_fstab(item: str) -> str:
    for tokens in _stripped_segment_tokens(item):
        tool = tokens[0].rsplit("/", 1)[-1].lower()
        if tool not in {"cp", "install", "mv"}:
            continue
        target = _last_non_option_path(tokens)
        if _path_is_exact_fstab(target):
            return _canonical_fstab_target(target)
    return ""


def _tee_writes_path(item: str, path_pattern: str) -> str:
    for match in re.finditer(
        r"\btee\b(?:\s+-[A-Za-z]+)*\s+[\"']?(?P<path>" + path_pattern + r")[\"']?",
        item,
        re.I,
    ):
        return _strip_path_quotes(match.group("path"))
    return ""


def _redirection_writes_path(item: str, path_pattern: str) -> str:
    for match in re.finditer(
        r"(?:^|[\s;|&])(?:>|>>)\s*[\"']?(?P<path>" + path_pattern + r")[\"']?",
        item,
        re.I,
    ):
        return _strip_path_quotes(match.group("path"))
    return ""


def _wipefs_match_mutates(match_text: str) -> bool:
    try:
        tokens = shlex.split(match_text, posix=True)
    except ValueError:
        tokens = match_text.split()
    lowered = [token.lower() for token in tokens]
    return "--no-act" not in lowered and "-n" not in lowered


def _readonly_mount(tokens: list[str]) -> bool:
    args = tokens[1:]
    for idx, arg in enumerate(args):
        if arg in {"-r", "--read-only"}:
            return True
        if arg == "-o" and idx + 1 < len(args):
            options = {item.strip().lower() for item in args[idx + 1].split(",")}
            if "ro" in options:
                return True
        if arg.startswith("-o") and len(arg) > 2:
            options = {item.strip().lower() for item in arg[2:].split(",")}
            if "ro" in options:
                return True
    return False


def _mount_capability_target(source: str, destination: str) -> str:
    source = _strip_path_quotes(source)
    destination = _strip_path_quotes(destination)
    if source:
        return f"{source} -> {destination}"
    return destination


def _bootloader_install_target(item: str) -> str:
    for tokens in _stripped_segment_tokens(item):
        for root, inner in _command_token_contexts(tokens):
            if not inner:
                continue
            tool = inner[0].rsplit("/", 1)[-1].lower()
            args = inner[1:]
            if tool in {"grub-install", "grub2-install"}:
                device = next(
                    (arg for arg in _non_option_args(args) if arg.startswith("/dev/")), ""
                )
                return root or device or "/boot"
            if tool == "bootctl" and "install" in _non_option_args(args):
                return root or _option_value(args, "--esp-path") or "/boot"
    return ""


def _bootloader_config_target(item: str) -> str:
    for tokens in _stripped_segment_tokens(item):
        for root, inner in _command_token_contexts(tokens):
            if not inner:
                continue
            tool = inner[0].rsplit("/", 1)[-1].lower()
            args = inner[1:]
            if tool in {"grub-mkconfig", "grub2-mkconfig"}:
                return _join_chroot_path(
                    root, _option_value(args, "-o", "--output") or "/boot/grub/grub.cfg"
                )
            if tool == "update-grub":
                return _join_chroot_path(root, "/boot/grub/grub.cfg")
    return ""


_BOOT_CONFIG_PATH_RE = r"(?:/etc/default/grub|/boot/(?:grub/grub\.cfg|loader/entries/[^\s;|&\"']+|efi/EFI/[^\s;|&\"']+))"


def _boot_config_path_mutation_target(item: str) -> str:
    tee_target = _tee_writes_path(item, _BOOT_CONFIG_PATH_RE)
    if tee_target:
        return tee_target
    redirect_target = _redirection_writes_path(item, _BOOT_CONFIG_PATH_RE)
    if redirect_target:
        return redirect_target
    if re.search(
        r"\b(?:sed\s+-i|perl\s+-pi|truncate\b|rm\s+-[A-Za-z]*f?[A-Za-z]*|dd\b[^\n;|&]*\bof=)[^\n;|&]*"
        + _BOOT_CONFIG_PATH_RE,
        item,
        re.I,
    ):
        match = re.search(_BOOT_CONFIG_PATH_RE, item, re.I)
        return _strip_path_quotes(match.group(0)) if match else "/boot"
    if re.search(r"\b(?:cp|install|mv)\b[^\n;|&]*" + _BOOT_CONFIG_PATH_RE, item, re.I):
        match = re.search(_BOOT_CONFIG_PATH_RE, item, re.I)
        return _strip_path_quotes(match.group(0)) if match else "/boot"
    return ""


def _firewall_or_vpn_mutates(item: str) -> bool:
    return bool(
        re.search(
            r"\bufw\b(?:\s+-[\w-]+)*\s+(?:enable|reload|allow|deny|reject|limit|delete|insert|prepend|default|reset)\b",
            item,
            re.I,
        )
        or re.search(r"\btailscale\s+up\b", item, re.I)
        or re.search(
            r"\b(?:systemctl\s+(?:enable|start|restart|--now\s+enable).*(?:mullvad|wg-quick|firewalld|nftables|ufw|openvpn)|wg-quick\s+up)\b",
            item,
            re.I,
        )
        or re.search(r"\bnft\s+(?:add|delete|destroy|flush|insert|replace)\b", item, re.I)
        or re.search(
            r"\b(?:iptables|ip6tables)\b[^\n;|&]*(?:\s-[ADIRFPNXZ]\b|--(?:append|delete|insert|replace|flush|policy|delete-chain|new-chain|zero)\b)",
            item,
        )
        or re.search(
            r"\bfirewall-cmd\b[^\n;|&]*(?:--reload|--complete-reload|--add-|--remove-|--set-default)",
            item,
            re.I,
        )
    )


def _ssh_or_network_continuity_mutates(item: str) -> bool:
    return bool(
        re.search(
            r"\bsystemctl\b[^\n;|&]*(?:stop|disable|mask|restart|try-restart|reload-or-restart)[^\n;|&]*(?:ssh|sshd|networkmanager|network-manager)\b",
            item,
            re.I,
        )
        or re.search(
            r"\bservice\s+(?:ssh|sshd|networkmanager|network-manager)\s+(?:stop|restart|reload|force-reload)\b",
            item,
            re.I,
        )
        or re.search(r"\bnmcli\s+(?:networking\s+off|connection\s+down)\b", item, re.I)
        or re.search(
            r"\bip\s+(?:link\s+set\b[^\n;|&]*\bdown\b|route\s+del\s+default|addr\s+flush)\b",
            item,
            re.I,
        )
    )


def _power_transition_mutates(item: str) -> bool:
    for segment in _command_segments(item):
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            tokens = segment.split()
        tokens = _strip_command_prefix(tokens)
        if not tokens:
            continue
        tool = tokens[0].rsplit("/", 1)[-1].lower()
        args = [arg.lower() for arg in tokens[1:]]
        if tool in {"reboot", "poweroff", "halt", "shutdown"}:
            return True
        if tool == "systemctl":
            non_option_args = [arg for arg in args if not arg.startswith("-")]
            verb = non_option_args[0] if non_option_args else ""
            if verb in {"reboot", "poweroff", "halt", "kexec", "soft-reboot"}:
                return True
        if tool in {"telinit", "init"} and args and args[0] in {"0", "6"}:
            return True
        if tool == "kexec" and any(arg in {"-e", "--exec"} for arg in args):
            return True
    return bool(re.search(r"(?:>\s*)?/proc/sysrq-trigger\b", item or "", re.I))


def _ambiguous_variable_requirement(item: str) -> list[dict[str, str]]:
    if not re.search(r"\$(?:\(|\{|[A-Za-z_])", item or ""):
        return []
    if re.search(r"\b(?:mkfs(?:\.[\w+-]+)?|mkswap)\b", item, re.I):
        return [
            {
                "kind": "format_filesystem",
                "target": "",
                "reason": "ambiguous variable filesystem format target",
            }
        ]
    if re.search(r"\bcryptsetup\b[^;&|]*\b(?:luksformat|erase|lukserase)\b", item, re.I):
        return [
            {
                "kind": "format_filesystem",
                "target": "",
                "reason": "ambiguous variable LUKS format/erase target",
            }
        ]
    if re.search(r"\bdd\b[^;&|]*\bof=", item, re.I):
        return [
            {
                "kind": "disk_partition",
                "target": "",
                "reason": "ambiguous variable raw block-device write target",
            }
        ]
    if re.search(r"\bmount\b(?!point\b)[^;&|]*" + _TARGET_ROOT_RE, item, re.I):
        target_match = re.search(_TARGET_ROOT_RE, item, re.I)
        return [
            {
                "kind": "mount_target",
                "target": target_match.group(0) if target_match else "",
                "reason": "ambiguous variable target-root mount source",
            }
        ]
    if re.search(
        r"\b(?:parted|cfdisk|gdisk|sgdisk|sfdisk|fdisk|wipefs|blkdiscard|pvcreate|pvremove|vgcreate|vgremove|lvcreate|lvremove|shred|fsck|e2fsck|xfs_repair|resize2fs|xfs_growfs|btrfs|zpool|update-initramfs|dracut|mkinitcpio|kernel-install)\b",
        item,
        re.I,
    ):
        return [
            {
                "kind": "disk_partition",
                "target": "",
                "reason": "ambiguous variable disk metadata mutation target",
            }
        ]
    if re.search(r"\befibootmgr\b", item, re.I):
        return [
            {
                "kind": "bootloader_install",
                "target": "/boot",
                "reason": "ambiguous variable EFI boot mutation target",
            }
        ]
    return []


def dangerous_command_requirements(command: str) -> list[dict[str, str]]:
    """Return capability requirements for dangerous shell snippets."""

    requirements: list[dict[str, str]] = []
    text = str(command or "")
    scan = _expanded_shell_scans(text)
    for raw_item in scan:
        item = _executable_shell_scan_text(raw_item)
        if not item:
            continue
        lowered = item.lower()
        before = len(requirements)
        for tokens in _stripped_segment_tokens(item):
            for root, command_tokens in _command_token_contexts(tokens):
                command_text = " ".join(command_tokens)
                partition_editor = _partition_editor_requirement(command_tokens, command_text)
                if partition_editor:
                    requirements.append(partition_editor)
                repair_target = _filesystem_repair_target(command_tokens, command_text)
                if repair_target:
                    requirements.append(
                        {
                            "kind": "filesystem_repair",
                            "target": repair_target,
                            "reason": "filesystem repair command",
                        }
                    )
                resize_target = _filesystem_resize_target(command_tokens, command_text)
                if resize_target:
                    requirements.append(
                        {
                            "kind": "filesystem_resize",
                            "target": resize_target,
                            "reason": "filesystem resize command",
                        }
                    )
                topology_requirement = _storage_topology_requirement(command_tokens, command_text)
                if topology_requirement:
                    requirements.append(topology_requirement)
                initramfs_target = _initramfs_rebuild_target(command_tokens, root)
                if initramfs_target:
                    requirements.append(
                        {
                            "kind": "initramfs_rebuild",
                            "target": initramfs_target,
                            "reason": "initramfs rebuild command",
                        }
                    )
                boot_artifact_target = _boot_artifact_update_target(command_tokens, root)
                if boot_artifact_target:
                    requirements.append(
                        {
                            "kind": "boot_artifact_update",
                            "target": boot_artifact_target,
                            "reason": "kernel boot artifact update",
                        }
                    )
        for match in re.finditer(
            r"\bmkfs(?:\.[\w+-]+)?\b(?:[^;&|]*)\s[\"']?(?P<target>" + _DEVICE_RE + r")[\"']?",
            item,
            re.I,
        ):
            requirements.append(
                {
                    "kind": "format_filesystem",
                    "target": match.group("target"),
                    "reason": "filesystem format command",
                }
            )
        for match in re.finditer(
            r"\bmkswap\b(?:[^;&|]*)\s[\"']?(?P<target>" + _DEVICE_RE + r")[\"']?",
            item,
            re.I,
        ):
            requirements.append(
                {
                    "kind": "format_filesystem",
                    "target": match.group("target"),
                    "reason": "swap format command",
                }
            )
        for match in re.finditer(
            r"\bcryptsetup\b[^;&|]*(?:luksformat|luksFormat|erase|luksErase)\b[^;&|]*[\"']?(?P<target>"
            + _DEVICE_RE
            + r")[\"']?",
            item,
            re.I,
        ):
            requirements.append(
                {
                    "kind": "format_filesystem",
                    "target": match.group("target"),
                    "reason": "LUKS format/erase command",
                }
            )
        for match in re.finditer(
            r"\bparted\b[^;&|]*[\"']?(?P<target>" + _DEVICE_RE + r")[\"']?[^;&|]*\brm\b",
            item,
            re.I,
        ):
            requirements.append(
                {
                    "kind": "disk_partition",
                    "target": match.group("target"),
                    "reason": "partition removal command",
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
        for match in re.finditer(
            r"\bparted\b[^;&|]*(?P<target>" + _DEVICE_RE + r")[^;&|]*\bset\b",
            item,
            re.I,
        ):
            requirements.append(
                {
                    "kind": "disk_partition",
                    "target": match.group("target"),
                    "reason": "partition flag mutation command",
                }
            )
        sfdisk_write = (
            re.search(r"\bsfdisk\b", item, re.I)
            and _first_device(item)
            and (
                "<<" in item
                or re.search(r"\b(?:printf|echo|cat)\b[^|]*\|\s*(?:sudo\s+)?sfdisk\b", item, re.I)
                or re.search(r"\bsfdisk\b(?![^;&|]*\s(?:--dump|-d|--list|-l)\b)", item, re.I)
            )
        )
        if sfdisk_write:
            requirements.append(
                {
                    "kind": "disk_partition",
                    "target": _first_device(item),
                    "reason": "sfdisk partition table write flow",
                }
            )
        fdisk_write = (
            re.search(r"\bfdisk\b", item, re.I)
            and _first_device(item)
            and (
                "<<" in item
                or re.search(r"\b(?:printf|echo|cat)\b[^|]*\|\s*(?:sudo\s+)?fdisk\b", item, re.I)
                or re.search(r"(?:\\n|\n)w(?:\\n|\n|$)", item)
            )
        )
        if fdisk_write and not re.search(r"\bfdisk\b[^;&|]*\s-l\b", item, re.I):
            requirements.append(
                {
                    "kind": "disk_partition",
                    "target": _first_device(item),
                    "reason": "interactive fdisk write flow",
                }
            )
        for match in re.finditer(
            r"\bwipefs\b[^;&|]*[\"']?(?P<target>" + _DEVICE_RE + r")[\"']?",
            item,
            re.I,
        ):
            if not _wipefs_match_mutates(match.group(0)):
                continue
            requirements.append(
                {
                    "kind": "disk_partition",
                    "target": match.group("target"),
                    "reason": "wipefs disk signature mutation",
                }
            )
        for tool in ("blkdiscard", "pvcreate", "pvremove", "shred"):
            for match in re.finditer(
                rf"\b{tool}\b[^;&|]*[\"']?(?P<target>{_DEVICE_RE})[\"']?",
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
            r"\b(?:vgcreate|lvcreate|lvremove|vgremove)\b[^;&|]*",
            item,
            re.I,
        ):
            requirements.append(
                {
                    "kind": "disk_partition",
                    "target": _first_device(match.group(0)),
                    "reason": "LVM metadata mutation",
                }
            )
        for match in re.finditer(r"\bzpool\s+destroy\b[^;&|]*(?P<target>[^\s;|&]+)", item, re.I):
            requirements.append(
                {
                    "kind": "disk_partition",
                    "target": match.group("target"),
                    "reason": "storage pool destroy",
                }
            )
        for match in re.finditer(
            r"\bdd\b[^;&|]*\bof=[\"']?(?P<target>" + _DEVICE_RE + r")[\"']?",
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
            + r"[\"']?"
            + _DEVICE_RE
            + r"[\"']?)[^;&|]*(?P<target>"
            + _INSTALL_MOUNTPOINT_RE
            + r")",
            item,
            re.I,
        ):
            mount_tokens = next(
                (tokens for tokens in _stripped_segment_tokens(match.group(0)) if tokens),
                [],
            )
            if mount_tokens and _readonly_mount(mount_tokens):
                continue
            requirements.append(
                {
                    "kind": "mount_target",
                    "target": _mount_capability_target(
                        match.group("source"),
                        match.group("target"),
                    ),
                    "reason": "target-root mount operation",
                }
            )
        for match in re.finditer(
            r"\bmount\b(?!point\b)[^;&|]*"
            + r"(?P<source>UUID=[^\s;|&\"']+|LABEL=[^\s;|&\"']+|-[UL]\s+[^\s;|&\"']+)"
            + r"[^;&|]*(?P<target>"
            + _INSTALL_MOUNTPOINT_RE
            + r")",
            item,
            re.I,
        ):
            requirements.append(
                {
                    "kind": "mount_target",
                    "target": _mount_capability_target(
                        match.group("source"),
                        match.group("target"),
                    ),
                    "reason": "target-root UUID/LABEL mount operation",
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
        if re.search(r"\befibootmgr\b[^\n;|&]*(?:\s-c\b|--create\b)", item, re.I):
            requirements.append(
                {
                    "kind": "bootloader_install",
                    "target": _first_device(item) or "/boot",
                    "reason": "EFI boot entry creation",
                }
            )
        if re.search(
            r"\befibootmgr\b[^\n;|&]*(?:\s-B\b|--delete-bootnum\b|\s-o\b|--bootorder\b|\s-n\b|--bootnext\b)",
            item,
            re.I,
        ):
            requirements.append(
                {
                    "kind": "bootloader_install",
                    "target": _first_device(item) or "/boot",
                    "reason": "EFI boot entry/order mutation",
                }
            )
        fstab_tee_target = _tee_writes_path(item, _FSTAB_PATH_RE)
        fstab_redirect_target = _redirection_writes_path(item, _FSTAB_PATH_RE)
        if fstab_tee_target or fstab_redirect_target:
            requirements.append(
                {
                    "kind": "write_fstab",
                    "target": _canonical_fstab_target(fstab_tee_target or fstab_redirect_target),
                    "reason": "fstab write",
                }
            )
        fstab_copy_target = _copy_like_writes_fstab(item)
        if fstab_copy_target:
            requirements.append(
                {
                    "kind": "write_fstab",
                    "target": fstab_copy_target,
                    "reason": "fstab file replacement",
                }
            )
        if re.search(
            r"\b(?:sed\s+-i|perl\s+-pi|truncate\b|rm\s+-[A-Za-z]*f[A-Za-z]*|dd\b[^\n;|&]*\bof=)[^\n;|&]*"
            + _FSTAB_PATH_RE,
            item,
            re.I,
        ) or re.search(
            r"\b(?:python3?|ruby|node)\b[^\n;|&]*(?:open|writeFileSync|File\.write)[^\n;|&]*"
            + _FSTAB_PATH_RE,
            item,
            re.I,
        ):
            requirements.append(
                {
                    "kind": "write_fstab",
                    "target": "/target-root/etc/fstab",
                    "reason": "fstab mutation",
                }
            )
        bootloader_install_target = _bootloader_install_target(item)
        if bootloader_install_target:
            requirements.append(
                {
                    "kind": "bootloader_install",
                    "target": bootloader_install_target,
                    "reason": "bootloader install",
                }
            )
        bootloader_config_target = _bootloader_config_target(item)
        if bootloader_config_target:
            requirements.append(
                {
                    "kind": "bootloader_config",
                    "target": bootloader_config_target,
                    "reason": "bootloader configuration mutation",
                }
            )
        boot_config_path_target = _boot_config_path_mutation_target(item)
        if boot_config_path_target:
            requirements.append(
                {
                    "kind": "bootloader_config",
                    "target": boot_config_path_target,
                    "reason": "boot configuration file mutation",
                }
            )
        if _power_transition_mutates(item):
            requirements.append(
                {
                    "kind": "controlled_reboot",
                    "target": "",
                    "reason": "controlled reboot or power-state transition",
                }
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
        if re.search(
            r"\bfind\b[^\n;|&]*"
            + _TARGET_ROOT_RE
            + r"[^\n;|&]*(?:-delete|-exec\s+(?:rm|shred|truncate)\b)",
            item,
            re.I,
        ):
            requirements.append(
                {
                    "kind": "disk_partition",
                    "target": "/target-root",
                    "reason": "recursive destructive find under target root",
                }
            )
        if _firewall_or_vpn_mutates(item) or _ssh_or_network_continuity_mutates(item):
            requirements.append(
                {
                    "kind": "network_security_change",
                    "target": "",
                    "reason": "network security activation/mutation",
                }
            )
        package_db_mutates = (
            re.search(
                r"(?:\bpacman\b[^\n;|&]*(?:\s-d\b|--dbonly\b)|(?:^|\s)-rdd(?:\s|$)|--nodeps\b)",
                lowered,
            )
            or re.search(r"\b(?:rm|shred)\b[^\n;|&]*" + _PACKAGE_DB_PATH_RE, item, re.I)
            or re.search(
                r"\bfind\b[^\n;|&]*"
                + _PACKAGE_DB_PATH_RE
                + r"[^\n;|&]*(?:-delete|-exec\s+(?:rm|shred|truncate)\b)",
                item,
                re.I,
            )
            or re.search(r"\b(?:cp|install|mv)\b[^\n;|&]*" + _PACKAGE_DB_PATH_RE, item, re.I)
            or re.search(
                r"\b(?:sed\s+-i|perl\s+-pi|truncate\b|dd\b[^\n;|&]*\bof=)[^\n;|&]*"
                + _PACKAGE_DB_PATH_RE,
                item,
                re.I,
            )
            or re.search(
                r"\b(?:python3?|ruby|node)\b[^\n;|&]*(?:open|writeFileSync|File\.write)"
                r"[^\n;|&]*" + _PACKAGE_DB_PATH_RE,
                item,
                re.I,
            )
        )
        rpm_rebuilds_db = re.search(r"\brpm\s+--rebuilddb\b", lowered)
        if rpm_rebuilds_db:
            requirements.append(
                {
                    "kind": "package_db_repair",
                    "target": "/var/lib/rpm",
                    "reason": "package database repair",
                }
            )
        if package_db_mutates:
            requirements.append(
                {
                    "kind": "package_db_repair",
                    "target": "/var/lib",
                    "reason": "package database repair",
                }
            )
        if len(requirements) == before:
            requirements.extend(_ambiguous_variable_requirement(item))
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
