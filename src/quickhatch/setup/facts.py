"""Typed runtime facts for the staged setup executor."""

from __future__ import annotations

from collections.abc import Callable

from .types import DesktopRuntimeState, DisplayRuntimeInfo, SetupFacts

_FACT_PREFIX = "QH_"


def setup_fact_probe_command() -> str:
    return r"""
echo 'QH_PM='$(if command -v pacman >/dev/null 2>&1; then echo pacman; elif command -v apt-get >/dev/null 2>&1 || command -v apt >/dev/null 2>&1; then echo apt; elif command -v dnf >/dev/null 2>&1; then echo dnf; elif command -v zypper >/dev/null 2>&1; then echo zypper; elif command -v apk >/dev/null 2>&1; then echo apk; elif command -v xbps-install >/dev/null 2>&1; then echo xbps; elif command -v emerge >/dev/null 2>&1; then echo emerge; else echo unknown; fi)
echo 'QH_AUR='$(if command -v yay >/dev/null 2>&1; then echo yay; elif command -v paru >/dev/null 2>&1; then echo paru; else echo none; fi)
echo 'QH_USER='$(whoami 2>/dev/null || echo unknown)
echo 'QH_LOGIN_USER='$(logname 2>/dev/null || whoami 2>/dev/null || echo unknown)
echo 'QH_IS_ROOT='$(if [ "$(id -u 2>/dev/null)" = "0" ]; then echo yes; else echo no; fi)
echo 'QH_SUDO_NOPASSWD='$(if [ "$(id -u 2>/dev/null)" = "0" ]; then echo yes; elif command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then echo yes; else echo no; fi)
echo 'QH_OS_ID='$(. /etc/os-release 2>/dev/null; echo ${ID:-unknown})
echo 'QH_OS_LIKE='$(. /etc/os-release 2>/dev/null; echo ${ID_LIKE:-})
echo 'QH_INIT='$(if command -v systemctl >/dev/null 2>&1; then echo systemd; elif command -v rc-service >/dev/null 2>&1; then echo openrc; elif command -v sv >/dev/null 2>&1; then echo runit; else echo unknown; fi)
echo 'QH_BOOT='$(if [ -d /sys/firmware/efi/efivars ]; then echo efi; else echo bios; fi)
echo 'QH_TARGET_ROOT='$(if mount | grep -Eq ' on /(mnt|target)( |$)'; then echo yes; else echo no; fi)
echo 'QH_SESSION='${XDG_SESSION_TYPE:-}
echo 'QH_DM='$(basename "$(readlink /etc/systemd/system/display-manager.service 2>/dev/null)" 2>/dev/null | sed 's/\.service$//' || true)
echo 'QH_DM_ENABLED='$(if command -v systemctl >/dev/null 2>&1 && systemctl is-enabled display-manager >/dev/null 2>&1; then echo yes; else echo no; fi)
echo 'QH_GPU_VENDORS='$(lspci 2>/dev/null | awk 'BEGIN{IGNORECASE=1} /vga|3d|display/ { if ($0 ~ /nvidia/) print "nvidia"; else if ($0 ~ /amd|advanced micro devices|radeon/) print "amd"; else if ($0 ~ /intel/) print "intel"; }' | sort -u | paste -sd',' -)
echo 'QH_GPU_INVENTORY='$(lspci 2>/dev/null | awk 'BEGIN{IGNORECASE=1} /vga|3d|display/ {sub(/^.*: /,""); gsub(/\|/, "/", $0); print $0}' | paste -sd'|' -)
echo 'QH_NVIDIA_SMI='$(if command -v nvidia-smi >/dev/null 2>&1; then if nvidia-smi >/dev/null 2>&1; then echo ok; else echo present_but_failing; fi; else echo missing; fi)
echo 'QH_DRIVER_MODULES='$(lsmod 2>/dev/null | awk '/^nvidia/ {print $1}' | paste -sd',' -)
echo 'QH_DISPLAYS='$(if command -v xrandr >/dev/null 2>&1; then xrandr --query 2>/dev/null | awk '/ connected/ {name=$1; primary=($0 ~ / primary / ? "primary" : "secondary"); mode=""; if (match($0, /[0-9]+x[0-9]+\+[0-9]+\+[0-9]+/)) { mode=substr($0, RSTART, RLENGTH); sub(/\+.*/, "", mode); } print name ":" primary ":" mode}' | paste -sd'|' -; fi)
"""


def _parse_display_entry(entry: str) -> DisplayRuntimeInfo | None:
    parts = (entry or "").split(":")
    if not parts or not parts[0].strip():
        return None
    return DisplayRuntimeInfo(
        name=parts[0].strip(),
        primary=len(parts) > 1 and parts[1].strip() == "primary",
        current_mode=parts[2].strip() if len(parts) > 2 else "",
    )


def _desktop_runtime_state(facts: SetupFacts) -> DesktopRuntimeState:
    if facts.session_type:
        return DesktopRuntimeState.LIVE_APPLY_SUCCEEDED
    if facts.display_manager_enabled:
        return DesktopRuntimeState.PENDING_RELOGIN
    if facts.gpu_vendors and "nvidia" in facts.gpu_vendors and facts.nvidia_smi_state not in {
        "ok",
        "missing",
    }:
        return DesktopRuntimeState.BLOCKED_DRIVER_OR_DISPLAY_STACK
    return DesktopRuntimeState.BLOCKED_NO_GUI_SESSION


def describe_fact_changes(before: SetupFacts | None, after: SetupFacts | None) -> str:
    if before is None or after is None:
        return ""
    updates: list[str] = []
    tracked = {
        "package_manager": "package manager",
        "community_helper": "community helper",
        "current_user": "current user",
        "sudo_nopasswd": "sudo NOPASSWD",
        "nvidia_smi_state": "nvidia-smi",
        "session_type": "session type",
        "display_manager": "display manager",
    }
    for key, label in tracked.items():
        old = getattr(before, key, None)
        new = getattr(after, key, None)
        if old != new:
            updates.append(f"{label}: {old!s} -> {new!s}")
    if before.gpu_vendors != after.gpu_vendors:
        updates.append(
            f"GPU vendors: {', '.join(before.gpu_vendors) or 'none'} -> {', '.join(after.gpu_vendors) or 'none'}"
        )
    if before.displays != after.displays:
        old_disp = ", ".join(d.name or "unknown" for d in before.displays) or "none"
        new_disp = ", ".join(d.name or "unknown" for d in after.displays) or "none"
        updates.append(f"displays: {old_disp} -> {new_disp}")
    return "; ".join(updates)


def probe_setup_facts(
    ssh_exec: Callable[[str, str, str, str | None, int, int], tuple[int, str, str]],
    host: str,
    user: str,
    key_path: str | None,
    *,
    port: int = 22,
    fallback: SetupFacts | None = None,
    is_live: bool = False,
    live_marker: str = "",
    pending_question_id: str = "",
    pending_question_blocking: bool = False,
) -> SetupFacts:
    base = fallback or SetupFacts(current_user=user, login_user=user)
    rc, out, _ = ssh_exec(host, user, setup_fact_probe_command(), key_path, 30, port)
    facts = SetupFacts(
        distro_family=base.distro_family,
        package_manager=base.package_manager,
        community_helper=base.community_helper,
        init_system=base.init_system,
        os_id=base.os_id,
        os_like=base.os_like,
        is_live=is_live,
        live_marker=live_marker,
        boot_mode=base.boot_mode,
        target_root_mounted=base.target_root_mounted,
        current_user=base.current_user or user,
        login_user=base.login_user or user,
        is_root=base.is_root,
        sudo_nopasswd=base.sudo_nopasswd,
        gpu_vendors=list(base.gpu_vendors),
        gpu_inventory=list(base.gpu_inventory),
        nvidia_smi_state=base.nvidia_smi_state,
        driver_modules=list(base.driver_modules),
        session_type=base.session_type,
        display_manager=base.display_manager,
        display_manager_enabled=base.display_manager_enabled,
        desktop_runtime_state=base.desktop_runtime_state,
        displays=list(base.displays),
        requested_app_states=dict(base.requested_app_states),
        pending_question_id=pending_question_id,
        pending_question_blocking=pending_question_blocking,
        waiting_for_input=bool(pending_question_id and pending_question_blocking),
        raw=dict(base.raw),
    )
    if rc != 0:
        return facts
    raw: dict[str, str] = {}
    for line in out.splitlines():
        if "=" not in line or not line.startswith(_FACT_PREFIX):
            continue
        key, value = line.split("=", 1)
        raw[key.strip()] = value.strip()
    facts.raw = dict(raw)
    facts.package_manager = raw.get("QH_PM", facts.package_manager) or facts.package_manager
    facts.community_helper = raw.get("QH_AUR", facts.community_helper) or facts.community_helper
    facts.current_user = raw.get("QH_USER", facts.current_user) or facts.current_user
    facts.login_user = raw.get("QH_LOGIN_USER", facts.login_user) or facts.login_user
    facts.is_root = raw.get("QH_IS_ROOT", "no") == "yes"
    facts.sudo_nopasswd = raw.get("QH_SUDO_NOPASSWD", "no") == "yes"
    facts.os_id = raw.get("QH_OS_ID", facts.os_id) or facts.os_id
    facts.os_like = raw.get("QH_OS_LIKE", facts.os_like) or facts.os_like
    facts.init_system = raw.get("QH_INIT", facts.init_system) or facts.init_system
    facts.boot_mode = raw.get("QH_BOOT", facts.boot_mode) or facts.boot_mode
    facts.target_root_mounted = raw.get("QH_TARGET_ROOT", "no") == "yes"
    facts.session_type = raw.get("QH_SESSION", facts.session_type) or ""
    facts.display_manager = raw.get("QH_DM", facts.display_manager) or ""
    facts.display_manager_enabled = raw.get("QH_DM_ENABLED", "no") == "yes"
    facts.gpu_vendors = [p for p in raw.get("QH_GPU_VENDORS", "").split(",") if p]
    facts.gpu_inventory = [p for p in raw.get("QH_GPU_INVENTORY", "").split("|") if p]
    facts.nvidia_smi_state = raw.get("QH_NVIDIA_SMI", facts.nvidia_smi_state) or facts.nvidia_smi_state
    facts.driver_modules = [p for p in raw.get("QH_DRIVER_MODULES", "").split(",") if p]
    facts.displays = [
        display
        for display in (_parse_display_entry(entry) for entry in raw.get("QH_DISPLAYS", "").split("|"))
        if display
    ]
    facts.desktop_runtime_state = _desktop_runtime_state(facts)
    return facts
