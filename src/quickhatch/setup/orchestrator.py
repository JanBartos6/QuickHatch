"""Stage-driven remote setup orchestration."""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from quickhatch.analysis.app_catalog import match_catalog_entry, selected_app_catalog_context
from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, UserProfile, generate_setup_password

from .attempts import build_attempt_signature, is_diagnostic_command
from .audit import (
    app_manual_followup_satisfied_by_verified,
    audit_stage,
    final_audit,
    findings_from_blockers,
)
from .capabilities import (
    DISK_INVENTORY_COMMAND,
    CapabilityStore,
    DangerousOperationPreview,
    build_operation_preview,
    canonicalize_target,
    dangerous_command_requirements,
    dual_boot_blocks_operation,
)
from .facts import describe_fact_changes
from .failure import classify_failure
from .families import get_family_installer
from .planner import (
    RepairActionValidationError,
    ResearchPlanValidationError,
    build_repair_prompt,
    build_research_prompt,
    extract_repair_action,
    extract_research_plan,
    model_json_loads,
)
from .preflight import (
    _split_command_segments,
    arch_community_helpers_in_command,
    preflight_setup_decisions,
)
from .resolver import build_install_command, nvidia_open_kernel_supported
from .runlog import SetupRunLogger, redact_sensitive_text, write_executed_commands_script
from .types import (
    AppPlanItem,
    AuditFinding,
    AuditFindingSeverity,
    AuditResult,
    CommandGroup,
    DesktopRuntimeState,
    FinalSetupStatus,
    PlanItemStatus,
    PreflightDecisionKind,
    ResearchPlan,
    SetupFacts,
    SetupStage,
)

_LIVE_DISK_PREP_BASE_COMMANDS = (
    "sudo swapoff -a || true",
    "mountpoint -q /mnt/sysimage && sudo umount -R /mnt/sysimage || true",
    "mountpoint -q /mnt/sysroot && sudo umount -R /mnt/sysroot || true",
    "mountpoint -q /mnt && sudo umount -R /mnt || true",
)
_LIVE_TARGET_ROOT_CANDIDATES = ("/mnt", "/target", "/mnt/sysroot", "/mnt/sysimage")
_LIVE_SESSION_USER_NAMES = {
    "root",
    "ubuntu",
    "liveuser",
    "user",
    "demo",
    "arch",
    "fedora",
    "mint",
    "manjaro",
    "kali",
}

_LIVE_DISK_DESTRUCTIVE_PATTERNS = (
    re.compile(r"\bparted\b.*\bmklabel\b", re.IGNORECASE),
    re.compile(r"\bparted\b.*\bmkpart\b", re.IGNORECASE),
    re.compile(r"\bmkfs(?:\.[\w+-]+)?\b", re.IGNORECASE),
    re.compile(r"\bwipefs\b", re.IGNORECASE),
    re.compile(r"\bsgdisk\b", re.IGNORECASE),
    re.compile(r"\bfdisk\b", re.IGNORECASE),
    re.compile(r"\b(?:anaconda|liveinst)\b.*(?:--kickstart|-C|--noninteractive)", re.IGNORECASE),
    re.compile(r"\b(?:clearpart|autopart|zerombr|liveimg)\b", re.IGNORECASE),
)
_LIVE_DISK_REREAD_REQUIRED_RE = re.compile(
    r"unable to inform the kernel of the change|"
    r"old partition\(s\) will remain in use|"
    r"you should reboot now before making further changes|"
    r"this disk is currently in use|"
    r"device or resource busy",
    re.IGNORECASE,
)
_LIVE_DISK_REREAD_COMMAND_RE = re.compile(
    r"\b(?:parted|sfdisk|fdisk|sgdisk|partprobe|partx|wipefs|mkfs(?:\.[\w+-]+)?)\b",
    re.IGNORECASE,
)
_ARCH_NVIDIA_INSTALL_RE = re.compile(
    r"\b(?:sudo\s+)?pacman\s+-S(?:y|u|yu|yyu)?\b.*\bnvidia\b",
    re.IGNORECASE,
)
_ARCH_NVIDIA_OPEN_PACKAGES = (
    "linux-headers",
    "nvidia-open",
    "nvidia-utils",
    "lib32-nvidia-utils",
    "nvidia-settings",
)
_ARCH_NVIDIA_LTS_OPEN_PACKAGES = (
    "linux-lts-headers",
    "nvidia-lts-open",
    "nvidia-utils",
    "lib32-nvidia-utils",
    "nvidia-settings",
)
_ARCH_NVIDIA_ZEN_OPEN_PACKAGES = (
    "linux-zen-headers",
    "nvidia-open-dkms",
    "nvidia-utils",
    "lib32-nvidia-utils",
    "nvidia-settings",
)
_ARCH_NVIDIA_HARDENED_OPEN_PACKAGES = (
    "linux-hardened-headers",
    "nvidia-open-dkms",
    "nvidia-utils",
    "lib32-nvidia-utils",
    "nvidia-settings",
)
_ARCH_ENABLE_MULTILIB_COMMAND = (
    "sudo sed -i "
    "'/^#\\?\\[multilib\\]/,/^#\\?Include = \\/etc\\/pacman.d\\/mirrorlist/s/^#//' "
    "/etc/pacman.conf"
)
_ARCH_AUR_HELPER_BOOTSTRAP_RE = re.compile(
    r"\baur\.archlinux\.org/(?:yay|yay-bin|paru|paru-bin)\.git\b",
    re.IGNORECASE,
)
_ARCH_NVIDIA_BOOT_CONFIG_COMMANDS = (
    "printf '%s\\n' 'blacklist nouveau' 'options nouveau modeset=0' | sudo tee /etc/modprobe.d/blacklist-nouveau.conf >/dev/null",
    "printf '%s\\n' 'options nvidia-drm modeset=1' | sudo tee /etc/modprobe.d/nvidia.conf >/dev/null",
    "sudo sed -i 's/^MODULES=.*/MODULES=(nvidia nvidia_modeset nvidia_uvm nvidia_drm)/' /etc/mkinitcpio.conf",
    'if [ -f /etc/default/grub ] && ! grep -q \'nvidia-drm.modeset=1\' /etc/default/grub; then sudo sed -i \'s/GRUB_CMDLINE_LINUX_DEFAULT="\\([^"]*\\)"/GRUB_CMDLINE_LINUX_DEFAULT="\\1 nvidia-drm.modeset=1"/\' /etc/default/grub; fi',
    'if [ -f /etc/default/grub ] && ! grep -q \'modprobe.blacklist=nouveau\' /etc/default/grub; then sudo sed -i \'s/GRUB_CMDLINE_LINUX_DEFAULT="\\([^"]*\\)"/GRUB_CMDLINE_LINUX_DEFAULT="\\1 modprobe.blacklist=nouveau"/\' /etc/default/grub; fi',
    "sudo mkinitcpio -P",
    "if command -v grub-mkconfig >/dev/null 2>&1; then sudo grub-mkconfig -o /boot/grub/grub.cfg; fi",
)
_ARCH_NVIDIA_MODULE_LOAD_COMMANDS = (
    "sudo modprobe nvidia || true",
    "sudo modprobe nvidia_uvm || true",
    "sudo modprobe nvidia_drm || true",
)
_UBUNTU_NVIDIA_PACKAGES = ("ubuntu-drivers-common", "nvidia-driver")
_UBUNTU_NVIDIA_COMMANDS = (
    "sudo apt-get update",
    "sudo apt-get install -y ubuntu-drivers-common",
    "sudo ubuntu-drivers install",
    "sudo update-initramfs -u -k all || true",
)
_DEBIAN_NVIDIA_PACKAGES = ("nvidia-driver", "firmware-misc-nonfree")
_DEBIAN_ENABLE_NONFREE_COMMAND = (
    "codename=$(. /etc/os-release && printf '%s' \"${VERSION_CODENAME:-stable}\"); "
    "if ! grep -Rqs 'non-free' /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null; "
    "then printf 'Types: deb\\nURIs: http://deb.debian.org/debian\\nSuites: %s %s-updates\\n"
    "Components: main contrib non-free non-free-firmware\\n"
    "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\\n\\n"
    "Types: deb\\nURIs: http://security.debian.org/debian-security\\nSuites: %s-security\\n"
    "Components: main contrib non-free non-free-firmware\\n"
    "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\\n' "
    '"$codename" "$codename" "$codename" | '
    "sudo tee /etc/apt/sources.list.d/quickhatch-nonfree.sources >/dev/null; fi"
)
_DEBIAN_NVIDIA_COMMANDS = (
    _DEBIAN_ENABLE_NONFREE_COMMAND,
    "sudo apt-get update",
    "sudo apt-get install -y nvidia-driver firmware-misc-nonfree",
    "sudo update-initramfs -u -k all || true",
)
_FEDORA_NVIDIA_PACKAGES = ("akmod-nvidia", "xorg-x11-drv-nvidia-cuda")
_FEDORA_NVIDIA_COMMANDS = (
    "sudo dnf install -y "
    "https://download1.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm "
    "https://download1.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-$(rpm -E %fedora).noarch.rpm",
    "sudo dnf install -y akmod-nvidia xorg-x11-drv-nvidia-cuda",
    "sudo akmods --force || true",
    "sudo dracut --force || true",
)
_OPENSUSE_NVIDIA_PACKAGES = (
    "nvidia-open-driver-G06-signed-kmp-meta",
    "nvidia-userspace-meta-G06",
)
_OPENSUSE_NVIDIA_COMMANDS = (
    "if grep -qi tumbleweed /etc/os-release; then "
    "sudo zypper --non-interactive install openSUSE-repos-Tumbleweed-NVIDIA; "
    "elif grep -qi slowroll /etc/os-release; then "
    "sudo zypper --non-interactive install openSUSE-repos-Slowroll-NVIDIA; "
    "else sudo zypper --non-interactive install openSUSE-repos-Leap-NVIDIA; fi",
    "sudo zypper --non-interactive --gpg-auto-import-keys refresh",
    "sudo zypper --non-interactive install nvidia-open-driver-G06-signed-kmp-meta nvidia-userspace-meta-G06",
    "sudo dracut -f --regenerate-all || true",
)
_VOID_NVIDIA_PACKAGES = ("void-repo-nonfree", "nvidia", "nvidia-libs-32bit")
_VOID_NVIDIA_COMMANDS = (
    "sudo xbps-install -Sy void-repo-nonfree",
    "sudo xbps-install -Syu nvidia nvidia-libs-32bit",
    "sudo xbps-reconfigure -f linux || true",
)
_NVIDIA_HARNESS_CAPABILITY_COMMANDS = (
    *_ARCH_NVIDIA_BOOT_CONFIG_COMMANDS,
    *_UBUNTU_NVIDIA_COMMANDS,
    *_DEBIAN_NVIDIA_COMMANDS,
    *_FEDORA_NVIDIA_COMMANDS,
    *_OPENSUSE_NVIDIA_COMMANDS,
    *_VOID_NVIDIA_COMMANDS,
)
_ARCH_AUR_BUILD_USER = "quickhatch-build"
_ARCH_YAY_BOOTSTRAP_COMMAND = (
    "(sudo timedatectl set-ntp true >/dev/null 2>&1 && sleep 5 || true) && "
    "sudo pacman -S --needed --noconfirm base-devel git && "
    "rm -rf /tmp/quickhatch-yay-bin && "
    "git clone https://aur.archlinux.org/yay-bin.git /tmp/quickhatch-yay-bin && "
    "cd /tmp/quickhatch-yay-bin && "
    "makepkg -s --noconfirm && "
    "sudo pacman -U --noconfirm yay-bin-*.pkg.tar.zst"
)
_ARCH_ROOT_YAY_BOOTSTRAP_COMMAND = (
    "set -e; "
    "(timedatectl set-ntp true >/dev/null 2>&1 && sleep 5 || true); "
    "pacman -S --needed --noconfirm base-devel git sudo && "
    f"id -u {_ARCH_AUR_BUILD_USER} >/dev/null 2>&1 || "
    f"useradd -m -G wheel -s /bin/bash {_ARCH_AUR_BUILD_USER}; "
    f"printf '%s\\n' '{_ARCH_AUR_BUILD_USER} ALL=(ALL:ALL) NOPASSWD: /usr/bin/pacman' "
    f"> /etc/sudoers.d/99-quickhatch-aur-build && "
    "chmod 440 /etc/sudoers.d/99-quickhatch-aur-build && "
    "rm -rf /tmp/quickhatch-yay-bin && "
    f"sudo -u {_ARCH_AUR_BUILD_USER} bash -lc "
    "'git clone https://aur.archlinux.org/yay-bin.git /tmp/quickhatch-yay-bin && "
    "cd /tmp/quickhatch-yay-bin && makepkg --noconfirm' && "
    "pacman -U --noconfirm /tmp/quickhatch-yay-bin/yay-bin-*.pkg.tar.zst"
)
_ARCH_LIVE_VCONSOLE_PREP_COMMAND = (
    "mkdir -p /mnt/etc && { test -f /mnt/etc/vconsole.conf || "
    "printf 'KEYMAP=us\\n' > /mnt/etc/vconsole.conf; }"
)
_ARCH_LIVE_PACSTRAP_KEYRING_COMMAND = (
    'sh -c \'set -e; qh_sudo=; [ "$(id -u)" = 0 ] || qh_sudo=sudo; '
    "$qh_sudo pacman-key --init; "
    "$qh_sudo pacman-key --populate archlinux; "
    "$qh_sudo pacman -Sy --noconfirm archlinux-keyring'"
)
_ARCH_PACSTRAP_MNT_RE = re.compile(r"\bpacstrap\b.*\s/mnt(?:\s|$)", re.IGNORECASE)
_SETUP_MODEL_CALL_TIMEOUT_SECONDS = max(
    30, int(os.environ.get("QUICKHATCH_SETUP_MODEL_TIMEOUT", "90") or "90")
)
_NON_BLOCKING_FAILED_SYSTEMD_UNITS = {
    "mcelog.service",
    "systemd-modules-load.service",
    "systemd-resolved.service",
    "whoopsie.path",
    "whoopsie.service",
}
_NON_BLOCKING_FAILED_SYSTEMD_UNIT_PREFIXES = (
    "drkonqi-coredump-processor@",
    "systemd-coredump@",
)
_DESKTOP_RUNTIME_COMMAND_RE = re.compile(
    r"\b(?:plasma-apply-[\w-]+|kscreen-doctor|xrandr|qdbus6?|gsettings|"
    r"lookandfeeltool|xfdesktop|nitrogen|feh|swww|hyprpaper)\b",
    re.IGNORECASE,
)
_DNF_CONFIG_MANAGER_ADD_REPO_RE = re.compile(
    r"^(?P<prefix>\s*(?:sudo\s+)?dnf5?\s+config-manager\s+)"
    r"--add-repo(?:=|\s+)(?P<url>\"[^\"]+\"|'[^']+'|\S+)(?P<rest>.*)$",
    re.IGNORECASE,
)


def is_non_blocking_failed_systemd_unit(unit: str) -> bool:
    """Return true for noisy/transient failed units that should not block setup.

    Crash-report/coredump helpers can be left failed by a previous user-session
    crash or disabled crash-report pipeline even when the installed OS is
    otherwise bootable and configured. Real service failures still remain hard
    blockers.
    """

    normalized = (unit or "").strip()
    return normalized in _NON_BLOCKING_FAILED_SYSTEMD_UNITS or normalized.startswith(
        _NON_BLOCKING_FAILED_SYSTEMD_UNIT_PREFIXES
    )


_STANDALONE_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*=\$\(.+\)\s*$"
    r"|^\s*(?:export\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s*)+\s*$"
)
_ASSIGNMENT_NAME_RE = re.compile(r"(?:^|[;&|]\s*)(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)=")
_STATEFUL_ASSIGNMENT_PREFIX_RE = re.compile(
    r"^\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*=", re.IGNORECASE
)
_STATEFUL_ASSIGNMENT_REAL_COMMAND_RE = re.compile(
    r"\b(?:"
    r"parted|mkfs(?:\.[A-Za-z0-9_-]+)?|mount|umount|pacstrap|genfstab|"
    r"arch-chroot|chroot|pacman|apt-get|apt|dnf5?|zypper|apk|xbps-install|"
    r"emerge|grub-install|grub-mkconfig|bootctl|systemctl|tee|printf|echo|"
    r"sed|cp|mv|rm|curl|wget|dd|wipefs|sgdisk"
    r")\b",
    re.IGNORECASE,
)
_INSTALL_ROOT_FSTAB_VERIFY_COMMAND = (
    "test -f /mnt/etc/fstab && awk "
    + shlex.quote('$1 !~ /^#/ && $2 == "/" { found=1 } END { exit(found ? 0 : 1) }')
    + " /mnt/etc/fstab"
)
_PREBOOT_DRIVER_RUNTIME_VERIFY_RE = re.compile(
    r"\b(?:nvidia-smi|vulkaninfo|lsmod)\b"
    r"|\bmodinfo\s+(?:nvidia|nvidia_drm|nvidia_uvm|nvidia_modeset|amdgpu|i915|nouveau)\b"
    r"|/sys/module/(?:nvidia|nvidia_drm|nvidia_uvm|nvidia_modeset|amdgpu|i915|nouveau)/",
    re.IGNORECASE,
)
_PACMAN_QI_SINGLE_RE = re.compile(
    r"^(?:sudo\s+)?pacman\s+-Qi\s+(?P<name>[A-Za-z0-9@._+:-]+)\s*$",
    re.IGNORECASE,
)
_SIMPLE_APP_LAUNCHER_PROBE_RE = re.compile(
    r"^\s*(?:command\s+-v|which)\s+(?P<binary>[A-Za-z0-9@._+-]+)\s*$"
    r"|^\s*(?P<binary2>[A-Za-z0-9@._+-]+)\s+(?:--version|-version|-v)\s*$",
    re.IGNORECASE,
)
_APT_EXTERNAL_REPO_SETUP_RE = re.compile(
    r"\b(?:curl|wget|gpg|apt-key|add-apt-repository)\b|/etc/apt/sources\.list\.d/|signed-by=",
    re.IGNORECASE,
)
_APT_NETWORK_OPTIONS = (
    "-o Acquire::Retries=2 "
    "-o Acquire::http::Timeout=20 "
    "-o Acquire::https::Timeout=20 "
    "-o Acquire::ForceIPv4=true"
)
_APT_COMMAND_RE = re.compile(
    r"(?<![\w/-])(?P<tool>apt-get|apt)(?![\w-])\s+(?![^;&|]*Acquire::)",
    re.IGNORECASE,
)
_APT_INSTALL_COMMAND_RE = re.compile(
    r"(?<![\w/-])(?:apt-get|apt)(?![\w-])(?:(?![;&|]).)*\binstall\b",
    re.IGNORECASE,
)
_APT_MUTATING_COMMAND_RE = re.compile(
    r"(?<![\w/-])(?P<sudo>sudo\s+)?(?P<tool>apt-get|apt)(?![\w-])"
    r"(?P<tail>(?:(?![;&|]).)*\b(?:install|dist-upgrade|full-upgrade|upgrade)\b"
    r"(?:(?![;&|]).)*)",
    re.IGNORECASE,
)
_KICKSTART_DIRECTIVE_IN_PRINTF_RE = re.compile(
    r"(?<!%)%(packages|end|post|pre|addon|include|ksappend)\b",
    re.IGNORECASE,
)
_PARTED_PRE_FORMAT_FS_CHECK_RE = re.compile(
    r"\bparted\s+-ms\s+(?P<disk>/\S+)\s+print\s*\|\s*grep\s+-q\s+"
    r"(?P<quote>['\"])\^(?P<partition>\d+):\.\*:(?:fat32|ext[234]|btrfs|xfs):(?P=quote)",
    re.IGNORECASE,
)
_DEBIAN_LIVE_DEBOOTSTRAP_RE = re.compile(r"\bdebootstrap\b", re.IGNORECASE)
_UBUNTU_DEBOOTSTRAP_FALLBACK_MIRROR = "http://archive.ubuntu.com/ubuntu"
_DEBIAN_DEBOOTSTRAP_FALLBACK_MIRROR = "http://deb.debian.org/debian"
_DEBOOTSTRAP_SHELL_CONTROL_TOKENS = {
    "case",
    "do",
    "done",
    "elif",
    "else",
    "esac",
    "fi",
    "for",
    "if",
    "then",
    "while",
}
_DEBOOTSTRAP_OPTION_ARGS_WITH_VALUE = {
    "-a",
    "--arch",
    "--components",
    "--exclude",
    "--foreign",
    "--include",
    "--keyring",
    "--merged-usr",
    "--no-merged-usr",
    "--variant",
}
_DEBIAN_LIVE_CHROOT_RE = re.compile(
    r"\b(?:chroot|systemd-nspawn)\s+(?P<target>/mnt|/target)\b", re.IGNORECASE
)
_DEBIAN_LIVE_BOOT_MOUNT_RE = re.compile(
    r"(?P<prefix>(?:sudo\s+)?mount\s+(?:\"[^\"]+\"|'[^']+'|\S+)\s+)/mnt/boot(?!/)\b",
    re.IGNORECASE,
)
_LIVE_NONROOT_CHROOT_RE = re.compile(
    r"(?P<prefix>^|[;&|]\s*)(?:sudo\s+)?"
    r"(?P<tool>arch-chroot|chroot|systemd-nspawn)\s+"
    r"(?P<target>/mnt|/target)\b",
    re.IGNORECASE,
)
_LIVE_RESOLV_CONF_TARGETS = (
    "/mnt/etc/resolv.conf",
    "/target/etc/resolv.conf",
    "/mnt/sysroot/etc/resolv.conf",
    "/mnt/sysimage/etc/resolv.conf",
)
_SYSFS_MODULE_PARAMETER_CAT_RE = re.compile(
    r"^(?P<prefix>\s*)cat\s+(?P<path>/sys/module/[A-Za-z0-9_+-]+/parameters/[A-Za-z0-9_+-]+)"
    r"(?P<rest>(?:\s|$).*)",
    re.IGNORECASE | re.DOTALL,
)

_REPO_APP_SOURCES = {
    "repo",
    "official",
    "official-repo",
    "official_repo",
    "official_docs",
    "repo_search",
    "selected_app_catalog",
    "distro_primer",
    "vendor_repo",
    "vendor-repo",
    "external_repo",
    "external-repo",
    "third_party_repo",
    "third-party-repo",
    "thirdparty_repo",
    "thirdparty-repo",
    "repo_external",
    "repo-external",
    "brave_repo",
    "brave-repo",
    "rpmfusion",
    "rpm_fusion",
    "rpm-fusion",
    "repo_rpmfusion",
    "repo-rpmfusion",
    "rpmfusion_repo",
    "rpmfusion-repo",
    "rpm_fusion_repo",
    "rpm-fusion-repo",
    "package_manager",
    "package-manager",
    "pacman",
    "apt",
    "apt-get",
    "apt_get",
    "dnf",
    "zypper",
    "apk",
    "xbps",
    "xbps-install",
    "xbps_install",
    "emerge",
    "core",
    "extra",
    "multilib",
}
_COMMUNITY_APP_SOURCES = {
    "community",
    "aur",
    "aur-binary",
    "aur_binary",
    "aur-source",
    "aur_source",
    "community_search",
    "yay",
    "paru",
}

_APP_PACKAGE_ALIASES: dict[str, set[str]] = {
    "brave": {"brave", "brave-bin", "brave-browser", "brave-browser-bin"},
    "brave browser": {"brave", "brave-bin", "brave-browser", "brave-browser-bin"},
    "cursor": {"cursor", "cursor-bin"},
    "github desktop": {"github-desktop", "github-desktop-bin"},
    "lm studio": {"lm-studio", "lm-studio-bin", "lmstudio", "lmstudio-bin"},
    "mozilla firefox": {"firefox", "org.mozilla.firefox"},
    "firefox": {"firefox", "org.mozilla.firefox"},
    "spotify": {"spotify", "spotify-client", "com.spotify.client"},
    "spotify music and podcasts": {"spotify", "spotify-client", "com.spotify.client"},
    "vlc": {"vlc", "org.videolan.vlc"},
    "vlc media player": {"vlc", "org.videolan.vlc"},
    "mullvad": {"mullvad", "mullvad-vpn", "mullvad-vpn-bin", "mullvad-vpn-daemon-bin"},
    "mullvad vpn": {
        "mullvad",
        "mullvad-vpn",
        "mullvad-vpn-bin",
        "mullvad-vpn-daemon-bin",
    },
    "localsend": {"localsend", "localsend-bin"},
    "local send": {"localsend", "localsend-bin"},
    "chatgpt": {"chatgpt", "chatgpt-desktop-bin", "chatgpt-desktop-unix-git", "pake-chatgpt"},
    "claude": {"claude", "claude-desktop-bin", "claude-desktop"},
    "codex": {"codex", "openai-codex", "openai-codex-desktop", "codex-desktop-bin"},
}
_WEAK_APP_BINARY_DESKTOP_ALIASES = {
    "app",
    "application",
    "code",
    "desktop",
    "github",
    "launcher",
    "manager",
    "python",
    "studio",
}


def _coerce_model_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "required"}:
        return True
    if text in {"0", "false", "no", "n", "off", "optional", "skip"}:
        return False
    return default


_INSTALLED_NONROOT_PRIVILEGED_STAGES = {
    SetupStage.POSTBOOT_DRIVER,
    SetupStage.DESKTOP,
    SetupStage.APPS,
}

_PRIVILEGED_COMMAND_RE = re.compile(
    r"^(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*"
    r"(?:"
    r"pacman|apt|apt-get|dnf|zypper|apk|xbps-install|emerge|"
    r"systemctl|service|rc-update|rc-service|"
    r"grub-mkconfig|grub2-mkconfig|grub-install|grub2-install|bootctl|"
    r"mkinitcpio|dracut|update-initramfs|update-grub|"
    r"modprobe|rmmod|depmod|"
    r"useradd|adduser|usermod|groupadd|groupmod|chpasswd|passwd|"
    r"timedatectl|hwclock"
    r")\b",
    re.IGNORECASE,
)

_PRIVILEGED_PATH_RE = re.compile(r"(?:^|\s)/(?:etc|boot|usr|var|opt)(?:/|\s|$)")
_PRIVILEGED_PATH_WRITE_RE = re.compile(
    r"^(?:"
    r"sed\s+-i|tee\b|install\b|mkdir\b|chmod\b|chown\b|ln\b|rm\b|cp\b|mv\b|touch\b"
    r")",
    re.IGNORECASE,
)
_INSTALLED_CHROOT_PREFIX_RE = re.compile(
    r"^(?:sudo\s+)?(?:arch-chroot|chroot)\s+/(?:mnt|target)\s+(?P<inner>.+)$",
    re.IGNORECASE | re.DOTALL,
)
_PRIVILEGED_REDIRECT_PREFIXES = ("/etc/", "/boot/", "/usr/", "/var/lib/", "/opt/")
_PRIVILEGED_REDIRECT_EXACT_PATHS = {"/etc", "/boot", "/usr", "/opt"}


def _preflight_decision_payload(decision: Any) -> dict[str, str]:
    kind = getattr(decision, "kind", "")
    return {
        "kind": getattr(kind, "value", str(kind)),
        "category": str(getattr(decision, "category", "")),
        "reason": str(getattr(decision, "reason", "")),
        "suggested_next": str(getattr(decision, "suggested_next", "")),
        "command": str(getattr(decision, "command", "")),
    }


@dataclass
class SetupRuntime:
    profile: UserProfile
    bridge: BridgeState
    config: AIConfig
    host: str
    user: str
    ssh_port: int
    key_path: str | None
    system_info_fn: Callable[[], str]
    call_model_text: Callable[..., str]
    probe_facts: Callable[..., SetupFacts]
    detect_live_env: Callable[[], tuple[bool, str]]
    ssh_exec: Callable[[str, int], tuple[int, str, str]]
    repo_search: Callable[[str], str]
    community_search: Callable[[str], str]
    web_search: Callable[[str, int], str]
    wait_for_reconnect: Callable[[str], tuple[bool, str, str]]
    run_pre_reboot_checks: Callable[[], tuple[bool, list[str], str]]
    smoke_test_preboot_script: Callable[[], str | None]
    bootstrap_nopasswd_sudo: Callable[[SetupFacts], SetupFacts]
    runtime_prompt_block: Callable[[SetupFacts, bool], str]
    migration_plan_md: str = ""
    distro_primer_md: str = ""
    target_distro_family: str = ""
    native_search_active: bool = False
    results_log_limit: int = 100
    copy_local_file_to_target: Callable[[Path, str, int], tuple[int, str, str]] | None = None
    executed_script_output_dir: Path | None = None


@dataclass
class SetupExecutionContext:
    run_id: str
    facts: SetupFacts | None = None
    system_info: str = ""
    plan: ResearchPlan | None = None
    stage_results: list[AuditResult] = field(default_factory=list)
    stage_state: dict[str, dict[str, Any]] = field(default_factory=dict)
    results_log: list[str] = field(default_factory=list)
    created_user: str | None = None
    capability_store: CapabilityStore | None = None
    last_disk_inventory: str = ""


@dataclass
class GroupFailure:
    failure_type: str
    command: str
    stdout: str
    stderr: str
    detail: str


@dataclass
class AppPackageResolution:
    app_name: str
    original_package: str
    package_name: str
    source: str
    command: str
    verification: str
    evidence_summary: str


@dataclass
class AppVerificationAliases:
    native_packages: set[str] = field(default_factory=set)
    flatpak_ids: set[str] = field(default_factory=set)
    snap_names: set[str] = field(default_factory=set)
    binaries: set[str] = field(default_factory=set)
    desktop_entries: set[str] = field(default_factory=set)


@dataclass
class AppVerificationMatch:
    app_name: str
    signal: str
    target: str
    alternative: bool = False
    alternative_name: str = ""


class SetupOrchestrator:
    """Execute staged remote setup with typed plans and audits."""

    DEFAULT_GROUP_REPAIRS = 3
    FINAL_AUDIT_REPAIRS = 2
    GROUP_REPAIR_BUDGETS = {
        SetupStage.INSTALL_OR_SKIP: 3,
        SetupStage.POSTBOOT_DRIVER: 6,
        SetupStage.DESKTOP: 10,
        SetupStage.APPS: 10,
    }
    REPAIR_BUDGET_EXTENSION_ATTEMPTS = 3
    MAX_REPAIR_TURNS = 8

    def __init__(self, runtime: SetupRuntime) -> None:
        self.runtime = runtime
        self.bridge = runtime.bridge
        self.profile = runtime.profile
        self.ctx = SetupExecutionContext(run_id=str(uuid.uuid4())[:8])
        self.ctx.capability_store = CapabilityStore(
            run_id=self.ctx.run_id,
            install_profile=getattr(self.profile, "install_profile", "auto") or "auto",
        )
        self.runlog = SetupRunLogger(self.ctx.run_id)

    def run(self) -> str:
        self._start_run_state()
        replay_status = "partial"
        try:
            try:
                self._run_connect_and_probe()
                self._run_research_and_plan()
                self._run_install_or_skip()
                self._run_postboot_driver()
                self._run_desktop()
                self._run_apps()
                final = self._run_final_audit()
                if not final.passed:
                    replay_status = "blocked"
                    summary = "Setup stopped during final audit: " + "; ".join(final.blockers)
                    self.bridge.add_message("agent", f"⚠️ {summary}")
                    return summary
                replay_status = "complete"
                summary = self._build_summary()
                self.bridge.add_message("agent", f"Finished: {summary}")
                return summary
            except RuntimeError as exc:
                stopped = self.bridge.stop_requested
                replay_status = "stopped" if stopped else "blocked"
                summary = str(exc) if stopped else f"Setup blocked: {exc}"
                if stopped and not summary:
                    summary = "Agent stopped by user."
                if stopped:
                    self._record_user_stop(reason=summary)
                    self.runlog.record(
                        "run_stopped",
                        status="stopped",
                        final_status="stopped",
                        stage=self.bridge.snapshot_setup_run().get("stage", ""),
                        reason=summary,
                    )
                self.bridge.update_setup_run(
                    status="stopped" if stopped else "blocked",
                    waiting_for_input=False,
                    active_question_id="",
                    blockers=[] if stopped else [str(exc)],
                )
                self.bridge.add_message(
                    "agent",
                    f"{'Stopped.' if stopped else '⚠️ ' + summary}",
                )
                return summary
        finally:
            self._maybe_write_executed_setup_script(replay_status)
            self.bridge.update_setup_run(waiting_for_input=False, active_question_id="")

    def _maybe_write_executed_setup_script(self, status: str) -> Path | None:
        if not getattr(self.profile, "export_executed_setup_script", False):
            return None
        path = write_executed_commands_script(
            self.runlog.path,
            output_dir=self.runtime.executed_script_output_dir,
            status=status,
        )
        if path is None:
            self.bridge.add_message(
                "agent",
                "⚠️ Could not write the executed-command replay script.",
            )
            self.runlog.record("executed_commands_script_failed", status=status)
            return None
        self.bridge.update_setup_run(executed_script_path=str(path))
        self.bridge.add_message(
            "agent",
            "Executed-command replay script saved: "
            f"`{path}`. Review before reuse; secrets are redacted.",
        )
        self.runlog.record("executed_commands_script_written", status=status, path=str(path))
        return path

    def _start_run_state(self) -> None:
        self.bridge.update_setup_run(
            run_id=self.ctx.run_id,
            stage=SetupStage.CONNECT_AND_PROBE.value,
            status="running",
            final_status="",
            blockers=[],
            waiting_for_input=False,
            active_question_id="",
            planning_complete=False,
            runlog_path=str(self.runlog.path),
        )
        self.runlog.record("run_started", host=self.runtime.host, user=self.runtime.user)

    def _record_user_stop(
        self,
        *,
        stage: SetupStage | None = None,
        group: CommandGroup | None = None,
        reason: str = "Agent stopped by user.",
    ) -> None:
        self.runlog.record(
            "user_stop",
            stage=stage.value if stage is not None else "",
            group_id=group.id if group is not None else "",
            group_title=group.title if group is not None else "",
            reason=reason,
        )

    def _raise_if_stop_requested(
        self,
        *,
        stage: SetupStage | None = None,
        group: CommandGroup | None = None,
        reason: str = "Agent stopped by user.",
    ) -> None:
        if not self.bridge.stop_requested:
            return
        self._record_user_stop(stage=stage, group=group, reason=reason)
        self.bridge.update_setup_run(
            status="stopped",
            waiting_for_input=False,
            active_question_id="",
            blockers=[],
        )
        raise RuntimeError(reason)

    def _record_stage_result(self, result: AuditResult) -> None:
        self.ctx.stage_results.append(result)
        self.bridge.update_setup_run(
            blockers=list(result.blockers),
            stage=result.stage.value,
            status="blocked" if not result.passed else "running",
        )
        self.runlog.record("stage_audit", stage=result.stage.value, result=asdict(result))
        if not result.passed:
            raise RuntimeError("; ".join(result.blockers))

    def _refresh_facts(self, *, stage: SetupStage) -> SetupFacts:
        before = self.ctx.facts
        is_live = self.ctx.facts.is_live if self.ctx.facts else False
        live_marker = self.ctx.facts.live_marker if self.ctx.facts else ""
        try:
            is_live, live_marker = self.runtime.detect_live_env()
        except Exception as exc:
            self.runlog.record(
                "live_env_refresh_failed",
                stage=stage.value,
                error=str(exc),
            )
        self.ctx.facts = self.runtime.probe_facts(
            fallback=self.ctx.facts,
            is_live=is_live,
            live_marker=live_marker,
            pending_question_id=self.bridge.snapshot_setup_run().get("active_question_id", ""),
            pending_question_blocking=self.bridge.snapshot_setup_run().get(
                "waiting_for_input", False
            ),
        )
        change_text = describe_fact_changes(before, self.ctx.facts)
        if change_text:
            self.bridge.add_message("agent", f"Refreshed setup facts: {change_text}")
        self.runlog.record("facts_refresh", stage=stage.value, facts=asdict(self.ctx.facts))
        return self.ctx.facts

    def _run_connect_and_probe(self) -> None:
        self.bridge.update_setup_run(stage=SetupStage.CONNECT_AND_PROBE.value, status="running")
        self.bridge.add_message(
            "agent",
            f"Connecting to {self.runtime.user}@{self.runtime.host}:{self.runtime.ssh_port}…",
        )
        rc, out, err = self.runtime.ssh_exec("echo 'QuickHatch connected!' && uname -a", 60)
        if rc != 0:
            raise RuntimeError(f"SSH connection failed: {err.strip()}")
        self.bridge.add_message("agent", f"Connected — {out.strip()}")
        preboot_err = self.runtime.smoke_test_preboot_script()
        if preboot_err:
            self.bridge.add_message("agent", f"⚠️ Pre-reboot guard self-test failed: {preboot_err}")
        else:
            self.bridge.add_message(
                "agent",
                "✓ Pre-reboot guard self-test passed — checks will run correctly at reboot time.",
            )
        self.ctx.system_info = self.runtime.system_info_fn()
        is_live, live_marker = self.runtime.detect_live_env()
        self.ctx.facts = self.runtime.probe_facts(
            fallback=None,
            is_live=is_live,
            live_marker=live_marker,
            pending_question_id="",
            pending_question_blocking=False,
        )
        if not self.ctx.facts.is_live:
            self.ctx.facts = self.runtime.bootstrap_nopasswd_sudo(self.ctx.facts)
        self.runlog.record(
            "connect_and_probe",
            system_info=self.ctx.system_info,
            facts=asdict(self.ctx.facts),
        )
        self._maybe_run_transfer_smoke_test()
        result = audit_stage(SetupStage.CONNECT_AND_PROBE, self.ctx.facts)
        self._record_stage_result(result)

    def _maybe_run_transfer_smoke_test(self) -> None:
        method = (self.profile.data_transfer_method or "").strip().lower()
        if method != "keep_windows":
            return
        state = self.ctx.stage_state.setdefault(SetupStage.CONNECT_AND_PROBE.value, {})
        target_path = f"/tmp/quickhatch-transfer-smoke-{self.ctx.run_id}.txt"
        if self.runtime.copy_local_file_to_target is None:
            reason = "host-side copy helper is unavailable"
            state["transfer_smoke_test"] = {"status": "skipped", "reason": reason}
            self.bridge.add_message(
                "agent",
                "⚠️ Network file-transfer smoke test skipped: "
                f"{reason}. Data migration must stay manual follow-up.",
            )
            self.runlog.record(
                "transfer_smoke_test",
                stage=SetupStage.CONNECT_AND_PROBE.value,
                status="skipped",
                reason=reason,
            )
            return

        payload = f"quickhatch-transfer-smoke {self.ctx.run_id}"
        local_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                newline="\n",
                prefix="quickhatch-transfer-smoke-",
                suffix=".txt",
                delete=False,
            ) as handle:
                handle.write(payload + "\n")
                local_path = Path(handle.name)
            rc, stdout, stderr = self.runtime.copy_local_file_to_target(local_path, target_path, 60)
            self.runlog.record(
                "transfer_smoke_copy",
                stage=SetupStage.CONNECT_AND_PROBE.value,
                target_path=target_path,
                rc=rc,
                stdout_tail=(stdout or "")[-500:],
                stderr_tail=(stderr or "")[-500:],
            )
            if rc != 0:
                detail = (stderr or stdout or "copy failed").strip()[:240]
                state["transfer_smoke_test"] = {"status": "failed", "detail": detail}
                self.bridge.add_message(
                    "agent",
                    "⚠️ Network file-transfer smoke test failed. "
                    "The install can continue, but Windows data transfer should be manual "
                    f"follow-up. {detail}",
                )
                return

            verify = (
                f"grep -qxF {shlex.quote(payload)} {shlex.quote(target_path)} "
                f"&& rm -f {shlex.quote(target_path)}"
            )
            verify_rc, verify_out, verify_err = self.runtime.ssh_exec(verify, 30)
            self.runlog.record(
                "transfer_smoke_verify",
                stage=SetupStage.CONNECT_AND_PROBE.value,
                command=verify,
                rc=verify_rc,
                stdout_tail=(verify_out or "")[-500:],
                stderr_tail=(verify_err or "")[-500:],
            )
            if verify_rc != 0:
                detail = (verify_err or verify_out or "verification failed").strip()[:240]
                state["transfer_smoke_test"] = {"status": "failed", "detail": detail}
                self.bridge.add_message(
                    "agent",
                    "⚠️ Network file-transfer smoke test copied a file, but remote "
                    f"verification failed. Treat data transfer as manual follow-up. {detail}",
                )
                return
            state["transfer_smoke_test"] = {"status": "passed", "target_path": target_path}
            self.bridge.add_message(
                "agent",
                "✓ Host-to-target file-transfer smoke test passed with a tiny dummy file.",
            )
        finally:
            if local_path is not None:
                try:
                    local_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _planner_model_call(self, messages: list[dict[str, str]]) -> str:
        planning_config = copy.copy(self.runtime.config)
        # The setup planner prompt is intentionally large because it carries
        # safety rules, target facts, distro primers, and command schemas.
        # Attaching provider-native search to that whole prompt can burn
        # grounding quota and make the call slow or flaky. Keep the user's
        # model/provider choice, but route current-fact lookups through the
        # planner's explicit repo/community/web search actions instead.
        planning_config.native_search = False
        return self._call_model_with_retries(planning_config, messages, context="planning")

    def _parse_planner_json(self, text: str) -> dict[str, Any]:
        raw = (text or "").strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(lines[1:-1] if len(lines) >= 2 else lines)
        try:
            data = model_json_loads(raw)
        except json.JSONDecodeError:
            start = raw.find("{")
            end = raw.rfind("}")
            if start < 0 or end <= start:
                raise
            data = model_json_loads(raw[start : end + 1])
        if not isinstance(data, dict):
            raise RuntimeError("Planner response must be a single JSON object.")
        return data

    def _run_research_and_plan(self) -> None:
        self.bridge.update_setup_run(stage=SetupStage.RESEARCH_AND_PLAN.value, status="running")
        self.bridge.add_message(
            "agent",
            "Researching package names, driver strategy, and stage plan before running commands…",
        )
        facts = self.ctx.facts or SetupFacts()
        runtime_guardrails = self.runtime.runtime_prompt_block(facts, facts.is_live)
        prompt = build_research_prompt(
            self.profile,
            facts,
            system_info=self.ctx.system_info,
            migration_plan_md=self.runtime.migration_plan_md,
            distro_primer_md=(
                self.runtime.distro_primer_md
                + (
                    "\n\n== SETUP RUNTIME GUARDRAILS ==\n" + runtime_guardrails
                    if runtime_guardrails
                    else ""
                )
            ),
            native_search_active=False,
            target_distro_family=self.runtime.target_distro_family,
        )
        self.runlog.record(
            "planner_prompt", stage=SetupStage.RESEARCH_AND_PLAN.value, prompt=prompt
        )
        messages = [{"role": "user", "content": prompt}]
        self.ctx.plan = None
        last_planner_error = ""
        lookup_history: list[dict[str, str]] = []
        lookup_budget = 3
        for _ in range(8):
            response_text = self._planner_model_call(messages)
            self.runlog.record(
                "planner_response",
                stage=SetupStage.RESEARCH_AND_PLAN.value,
                text=response_text,
            )
            try:
                payload = self._parse_planner_json(response_text)
            except json.JSONDecodeError as exc:
                last_planner_error = f"Planner returned invalid JSON: {exc}"
                self.runlog.record(
                    "planner_response_invalid",
                    stage=SetupStage.RESEARCH_AND_PLAN.value,
                    error=last_planner_error,
                )
                messages = self._planner_retry_messages(
                    prompt,
                    response_text,
                    (
                        "[quickhatch planner parser]\n"
                        f"{last_planner_error}\n"
                        "Return exactly one complete JSON object. Do not use Markdown fences, "
                        "comments, prose before/after JSON, or truncated strings."
                    ),
                )
                continue
            action = str(payload.get("action", "")).strip()
            if not action:
                try:
                    self.ctx.plan = extract_research_plan(
                        response_text,
                        self.profile,
                        provider_native_search_active=False,
                    )
                except ResearchPlanValidationError as exc:
                    last_planner_error = f"Research plan validation failed: {exc}"
                    self.runlog.record(
                        "planner_response_invalid",
                        stage=SetupStage.RESEARCH_AND_PLAN.value,
                        error=last_planner_error,
                    )
                    messages = self._planner_retry_messages(
                        prompt,
                        response_text,
                        (
                            "[quickhatch planner validator]\n"
                            f"{last_planner_error}\n"
                            "Fix the same plan and return JSON only. Keep all command groups "
                            "inside canonical stages and make every command/verification field "
                            "a shell command or list of shell commands."
                        ),
                    )
                    continue
                break
            if action == "plan":
                try:
                    self.ctx.plan = extract_research_plan(
                        json.dumps(payload.get("plan", {})),
                        self.profile,
                        provider_native_search_active=False,
                    )
                except ResearchPlanValidationError as exc:
                    last_planner_error = f"Research plan validation failed: {exc}"
                    self.runlog.record(
                        "planner_response_invalid",
                        stage=SetupStage.RESEARCH_AND_PLAN.value,
                        error=last_planner_error,
                    )
                    messages = self._planner_retry_messages(
                        prompt,
                        response_text,
                        (
                            "[quickhatch planner validator]\n"
                            f"{last_planner_error}\n"
                            "Fix the plan object and return JSON only. Use action=plan with a "
                            "valid plan, or return the valid plan object directly."
                        ),
                    )
                    continue
                break
            if action in {"repo_search", "community_search", "web_search"}:
                query = str(payload.get("query", "")).strip()
                if len(lookup_history) >= lookup_budget:
                    last_planner_error = "Planner lookup budget exhausted before final plan."
                    self.runlog.record(
                        "planner_lookup_budget_exhausted",
                        stage=SetupStage.RESEARCH_AND_PLAN.value,
                        action=action,
                        query=query,
                        lookup_count=len(lookup_history),
                    )
                    messages = self._planner_retry_messages(
                        prompt,
                        response_text,
                        (
                            "[quickhatch planner lookup budget]\n"
                            "You already have enough lookup evidence for this run. "
                            "Do not request another repo_search, community_search, or web_search. "
                            "Return action=plan now, or ask only a genuinely blocking safety question.\n\n"
                            + self._planner_lookup_history_text(lookup_history)
                        ),
                    )
                    continue
                result = self._planner_lookup_result(action, query, payload)
                lookup_history.append({"action": action, "query": query, "result": result})
            elif action == "question":
                required = _coerce_model_bool(payload.get("required", True), default=True)
                answer = self._ask_blocking_question(
                    str(payload.get("question", "")).strip(),
                    choices=payload.get("choices"),
                    stage=SetupStage.RESEARCH_AND_PLAN,
                    context={"planner_question": True, "required": required},
                    timeout=float(payload.get("timeout_seconds", 600) or 600),
                )
                if required and not str(answer or "").strip():
                    self.runlog.record(
                        "planner_required_question_unanswered",
                        stage=SetupStage.RESEARCH_AND_PLAN.value,
                        question=str(payload.get("question", "")).strip(),
                    )
                    raise RuntimeError(
                        "Required planning question was not answered; setup cannot continue safely."
                    )
                result = f"[question answer]\n{answer or 'no answer'}"
            else:
                last_planner_error = f"Unsupported planner action: {action}"
                messages = self._planner_retry_messages(
                    prompt,
                    response_text,
                    (
                        "[quickhatch planner parser]\n"
                        f"{last_planner_error}\n"
                        "Allowed actions are repo_search, community_search, web_search, "
                        "question, or plan. Return one valid JSON object."
                    ),
                )
                continue
            if lookup_history and action in {"repo_search", "community_search", "web_search"}:
                remaining = max(lookup_budget - len(lookup_history), 0)
                result = (
                    result
                    + "\n\n[quickhatch planner lookup history]\n"
                    + self._planner_lookup_history_text(lookup_history)
                    + "\n\n[quickhatch planner lookup budget]\n"
                    + (
                        "Lookup budget is exhausted. Next response must be action=plan "
                        "unless a genuinely blocking safety question remains."
                        if remaining == 0
                        else f"{remaining} lookup action(s) remain before you must return action=plan."
                    )
                )
            messages = [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": response_text},
                {"role": "user", "content": result},
            ]
        if self.ctx.plan is None:
            raise RuntimeError(
                last_planner_error or "Planner did not produce a final research plan."
            )
        self.bridge.update_setup_run(planning_complete=True)
        for question in self.ctx.plan.unresolved_questions:
            if not _coerce_model_bool(question.get("required", True), default=True):
                continue
            answer = self._ask_blocking_question(
                str(question.get("question", "")).strip(),
                choices=question.get("choices"),
                stage=SetupStage.RESEARCH_AND_PLAN,
                context={"question": question},
                timeout=float(question.get("timeout_seconds", 600) or 600),
            )
            if not str(answer or "").strip():
                self.runlog.record(
                    "planner_required_question_unanswered",
                    stage=SetupStage.RESEARCH_AND_PLAN.value,
                    question=str(question.get("question", "")).strip(),
                )
                raise RuntimeError(
                    "Required planning question was not answered; setup cannot continue safely."
                )
            question["answer"] = answer
        result = audit_stage(
            SetupStage.RESEARCH_AND_PLAN,
            self.ctx.facts or SetupFacts(),
            plan=self.ctx.plan,
        )
        self._record_stage_result(result)

    @staticmethod
    def _planner_retry_messages(
        prompt: str, response_text: str, correction: str
    ) -> list[dict[str, str]]:
        return [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": response_text},
            {"role": "user", "content": correction},
        ]

    @staticmethod
    def _bounded_lookup_max_results(raw_value: Any) -> int:
        try:
            value = int(raw_value or 5)
        except (TypeError, ValueError):
            value = 5
        return min(max(value, 1), 10)

    def _planner_lookup_result(self, action: str, query: str, payload: dict[str, Any]) -> str:
        try:
            if action == "repo_search":
                return self.runtime.repo_search(query)
            if action == "community_search":
                return self.runtime.community_search(query)
            if action == "web_search":
                return self.runtime.web_search(
                    query,
                    self._bounded_lookup_max_results(payload.get("max_results", 5)),
                )
        except Exception as exc:
            error_text = redact_sensitive_text(str(exc))
            self.runlog.record(
                "planner_lookup_failed",
                stage=SetupStage.RESEARCH_AND_PLAN.value,
                action=action,
                query=query,
                error=error_text,
            )
            return (
                f"[{action} failed]\n"
                f"query={query}\n"
                f"error={error_text}\n"
                "Search failed. If the package/source cannot be verified from existing "
                "facts or prior lookup history, return a plan that marks the affected "
                "item manual_followup instead of claiming success."
            )
        raise RuntimeError(f"Unsupported planner lookup action: {action}")

    @staticmethod
    def _planner_lookup_history_text(history: list[dict[str, str]]) -> str:
        lines: list[str] = []
        for index, item in enumerate(history, start=1):
            action = str(item.get("action") or "lookup")
            query = str(item.get("query") or "")
            result = re.sub(r"\s+", " ", str(item.get("result") or "").strip())
            if len(result) > 800:
                result = result[:800] + "..."
            lines.append(f"{index}. {action} query={query!r}: {result or '<no result>'}")
        return "\n".join(lines)

    def _iter_stage_groups(self, stage: SetupStage) -> list[CommandGroup]:
        if not self.ctx.plan:
            return []
        return [group for group in self.ctx.plan.command_groups if group.stage == stage]

    def _group_repair_budget(self, stage: SetupStage) -> int:
        return int(self.GROUP_REPAIR_BUDGETS.get(stage, self.DEFAULT_GROUP_REPAIRS))

    def _ask_repair_budget_extension(
        self,
        stage: SetupStage,
        group: CommandGroup,
        failure: GroupFailure,
        *,
        attempts_used: int,
        current_budget: int,
    ) -> str:
        extension = self.REPAIR_BUDGET_EXTENSION_ATTEMPTS
        choices = [f"try {extension} more repairs", "stop setup"]
        if stage not in {SetupStage.INSTALL_OR_SKIP}:
            choices.insert(1, "manual follow-up")
        answer = self._ask_blocking_question(
            (
                f"'{group.title}' has failed after {attempts_used} repair attempt(s) "
                f"in stage '{stage.value}'.\n\n"
                f"Last failure: {failure.detail}\n\n"
                f"Current repair budget: {current_budget}. Continue only if you want "
                "QuickHatch to spend more attempts on this exact group."
            ),
            choices=choices,
            stage=stage,
            context={
                "group_id": group.id,
                "repair_budget_exhausted": True,
                "attempts_used": attempts_used,
                "current_budget": current_budget,
            },
            timeout=1800,
            return_qid=False,
        )
        normalized = (answer or "").strip().lower()
        self.runlog.record(
            "repair_budget_gate_answer",
            stage=stage.value,
            group_id=group.id,
            attempts_used=attempts_used,
            current_budget=current_budget,
            answer=answer,
        )
        if normalized.startswith(("try", "continue", "yes", "y")):
            return "extend"
        if normalized.startswith("manual") and "manual follow-up" in choices:
            return "manual_followup"
        return "stop"

    def _run_stage_groups(self, stage: SetupStage, *, audit_after: bool = True) -> None:
        if self.ctx.facts is None or self.ctx.plan is None:
            raise RuntimeError("Setup orchestration missing facts or plan.")
        self.bridge.update_setup_run(stage=stage.value, status="running")
        stage_state = self.ctx.stage_state.setdefault(stage.value, {})
        if stage in _INSTALLED_NONROOT_PRIVILEGED_STAGES and self.ctx.facts.is_live:
            reason = (
                f"Stage '{stage.value}' cannot run while the target still reports a live "
                "environment. Finish the install, controlled reboot, reconnect, and fresh "
                "fact probe before running postboot, desktop, or app work."
            )
            stage_state["live_environment_stage_block"] = reason
            self.runlog.record(
                "postboot_stage_blocked_live_environment",
                stage=stage.value,
                reason=reason,
                facts=asdict(self.ctx.facts),
            )
            self.bridge.update_setup_run(status="blocked", blockers=[reason])
            self.bridge.add_message("agent", f"🛑 Safe stage block: {reason}")
            raise RuntimeError(reason)
        for group in self._iter_stage_groups(stage):
            self._raise_if_stop_requested(stage=stage, group=group)
            current_group = group
            repair_attempts = 0
            repair_budget = self._group_repair_budget(stage)
            while True:
                if stage == SetupStage.APPS:
                    filtered_group = self._filter_installed_app_group(current_group, stage_state)
                    if filtered_group is None:
                        current_group.status = PlanItemStatus.COMPLETED
                        self._mark_group_completion(current_group, stage=stage)
                        break
                    current_group = filtered_group
                if stage == SetupStage.DESKTOP and not self._desktop_runtime_ready_or_followup(
                    current_group, stage_state
                ):
                    current_group.status = PlanItemStatus.MANUAL_FOLLOWUP
                    reason = stage_state.get(
                        "display_manual_followup_reason",
                        "Display-dependent desktop work needs an active target monitor/session.",
                    )
                    self._mark_group_manual_followup(
                        current_group,
                        stage=stage,
                        reason=str(reason),
                        app_names=[],
                    )
                    self.runlog.record(
                        "group_manual_followup",
                        stage=stage.value,
                        group_id=current_group.id,
                        reason=reason,
                    )
                    break
                if current_group.optional_slow:
                    answer = self._ask_optional_slow_confirmation(current_group)
                    if answer is None:
                        current_group.status = PlanItemStatus.MANUAL_FOLLOWUP
                        self._mark_optional_apps_manual_followup(current_group)
                        self.runlog.record(
                            "group_manual_followup",
                            stage=stage.value,
                            group_id=current_group.id,
                            reason="User skipped optional slow install during setup.",
                        )
                        break
                failure = self._execute_group(stage, current_group, stage_state)
                if failure is None:
                    self._refresh_facts(stage=stage)
                    if stage == SetupStage.DESKTOP:
                        feedback_failure = self._desktop_design_feedback_failure(
                            current_group,
                            stage_state,
                        )
                        if feedback_failure is not None:
                            failure = feedback_failure
                        else:
                            current_group.status = PlanItemStatus.COMPLETED
                            self._mark_group_completion(current_group, stage=stage)
                            break
                    else:
                        current_group.status = PlanItemStatus.COMPLETED
                        self._mark_group_completion(current_group, stage=stage)
                        break
                self._raise_if_stop_requested(stage=stage, group=current_group)
                if failure.failure_type == "safe_abort":
                    raise RuntimeError(
                        str(stage_state.get("safe_abort") or failure.detail or "Safe abort.")
                    )
                if failure.failure_type == "live_reboot_required":
                    raise RuntimeError(failure.detail)
                auto_repaired = self._auto_repair_arch_pacman_fetch_failure(
                    stage,
                    current_group,
                    failure,
                    stage_state,
                )
                if auto_repaired is not None:
                    current_group = auto_repaired
                    continue
                auto_repaired = self._auto_repair_arch_aur_pgp_key_failure(
                    stage,
                    current_group,
                    failure,
                    stage_state,
                )
                if auto_repaired is not None:
                    current_group = auto_repaired
                    continue
                auto_repaired = self._auto_repair_missing_repo_targets(
                    stage, current_group, failure
                )
                if auto_repaired is not None:
                    current_group = auto_repaired
                    continue
                attempt_decision = self._register_repair_attempt_signature(
                    stage,
                    current_group,
                    failure,
                    stage_state,
                )
                if attempt_decision["loop_exhausted"]:
                    reason = (
                        "Repeated identical repair loop detected for "
                        f"'{current_group.title}': {attempt_decision['summary']}"
                    )
                    if stage in {
                        SetupStage.APPS,
                        SetupStage.DESKTOP,
                        SetupStage.POSTBOOT_DRIVER,
                    }:
                        current_group.status = PlanItemStatus.MANUAL_FOLLOWUP
                        self._mark_group_manual_followup(
                            current_group,
                            stage=stage,
                            reason=reason,
                            app_names=[],
                        )
                        self.bridge.add_message(
                            "agent",
                            f"⚠️ {reason}. Leaving this non-install group as manual follow-up.",
                        )
                        break
                    raise RuntimeError(reason)
                if attempt_decision["consumes_budget"]:
                    repair_attempts += 1
                if repair_attempts > repair_budget:
                    decision = self._ask_repair_budget_extension(
                        stage,
                        current_group,
                        failure,
                        attempts_used=repair_attempts - 1,
                        current_budget=repair_budget,
                    )
                    if decision == "extend":
                        repair_budget += self.REPAIR_BUDGET_EXTENSION_ATTEMPTS
                        self.runlog.record(
                            "repair_budget_extended",
                            stage=stage.value,
                            group_id=current_group.id,
                            repair_attempts=repair_attempts,
                            new_budget=repair_budget,
                        )
                    elif decision == "manual_followup":
                        if stage == SetupStage.INSTALL_OR_SKIP:
                            raise RuntimeError(
                                "Live install/base bootstrapping cannot be deferred to manual "
                                "follow-up inside the automated install stage. Stop, repair the "
                                "install command group, or ask the user before rebooting."
                            )
                        current_group.status = PlanItemStatus.MANUAL_FOLLOWUP
                        self._mark_group_manual_followup(
                            current_group,
                            stage=stage,
                            reason=(
                                f"User chose manual follow-up after {repair_attempts - 1} "
                                f"failed repair attempt(s): {failure.detail}"
                            ),
                            app_names=[],
                        )
                        break
                    else:
                        raise RuntimeError(
                            f"Stage {stage.value} exhausted repair attempts for "
                            f"'{current_group.title}': {failure.detail}"
                        )
                if repair_attempts > repair_budget:
                    raise RuntimeError(
                        f"Stage {stage.value} exhausted repair attempts for '{current_group.title}': {failure.detail}"
                    )
                current_group.status = PlanItemStatus.FAILED
                display_attempt = max(1, repair_attempts)
                self.bridge.add_message(
                    "agent",
                    (
                        f"Repairing '{current_group.title}' after {failure.failure_type} "
                        f"failure (attempt {display_attempt}/{repair_budget})…"
                    ),
                )
                repair_outcome = self._repair_failed_group(
                    stage,
                    current_group,
                    failure,
                    repair_attempt=display_attempt,
                )
                action = str(repair_outcome.get("action", ""))
                if action == "retry_group":
                    current_group = repair_outcome["group"]
                    continue
                if action == "manual_followup":
                    if stage == SetupStage.INSTALL_OR_SKIP:
                        raise RuntimeError(
                            "Live install/base bootstrapping cannot be marked manual follow-up; "
                            "the target would not be boot/reconnect safe."
                        )
                    reason = str(repair_outcome.get("reason", "") or failure.detail)
                    if stage == SetupStage.APPS:
                        missing = self._missing_repo_targets_from_output(
                            failure.stdout,
                            failure.stderr,
                            failure.detail,
                        )
                        if missing:
                            partial_repair = self._auto_repair_missing_app_targets(
                                current_group,
                                failure,
                                missing,
                                manual_after_repair=True,
                                manual_reason=reason,
                            )
                            if partial_repair is not None:
                                current_group = partial_repair
                                continue
                    current_group.status = PlanItemStatus.MANUAL_FOLLOWUP
                    self._mark_group_manual_followup(
                        current_group,
                        stage=stage,
                        reason=reason,
                        app_names=list(repair_outcome.get("app_names", []) or []),
                    )
                    self.runlog.record(
                        "group_manual_followup",
                        stage=stage.value,
                        group_id=current_group.id,
                        reason=reason,
                    )
                    break
                raise RuntimeError(
                    f"Unsupported repair outcome for {current_group.title}: {action}"
                )
        if audit_after:
            if stage == SetupStage.DESKTOP:
                self._update_desktop_display_audit_state(stage_state)
            if stage == SetupStage.APPS:
                self._mark_unanswered_optional_slow_apps_manual_followup()
            result = audit_stage(stage, self.ctx.facts, plan=self.ctx.plan, stage_state=stage_state)
            self._record_stage_result(result)

    def _register_repair_attempt_signature(
        self,
        stage: SetupStage,
        group: CommandGroup,
        failure: GroupFailure,
        stage_state: dict[str, Any],
    ) -> dict[str, Any]:
        signature = build_attempt_signature(
            stage=stage.value,
            group_id=group.id,
            group_title=group.title,
            command=failure.command,
            stdout=failure.stdout,
            stderr=failure.stderr,
            detail=failure.detail,
        )
        diagnostic = is_diagnostic_command(failure.command)
        loop_key = "|".join(str(part) for part in signature.loop_key)
        counts = stage_state.setdefault("attempt_loop_counts", {})
        count = int(counts.get(loop_key, 0) or 0) + 1
        counts[loop_key] = count
        consumes_budget = not diagnostic or count >= 3
        loop_exhausted = count >= 3 and not diagnostic
        summary = (
            f"{signature.failure_category}/{signature.operation}/{signature.tool}"
            + (f" packages={','.join(signature.packages)}" if signature.packages else "")
            + (f" target={signature.target}" if signature.target else "")
            + f" strategy={signature.strategy}"
        )
        self.runlog.record(
            "repair_attempt_signature",
            stage=stage.value,
            group_id=group.id,
            command=failure.command,
            signature=asdict(signature),
            loop_key=list(signature.loop_key),
            same_loop_count=count,
            diagnostic=diagnostic,
            consumes_budget=consumes_budget,
            loop_exhausted=loop_exhausted,
        )
        return {
            "signature": signature,
            "same_loop_count": count,
            "diagnostic": diagnostic,
            "consumes_budget": consumes_budget,
            "loop_exhausted": loop_exhausted,
            "summary": summary,
        }

    def _run_install_or_skip(self) -> None:
        if self.ctx.facts and not self.ctx.facts.is_live:
            self.ctx.stage_state[SetupStage.INSTALL_OR_SKIP.value] = {
                "pre_reboot_checks_passed": True,
                "reconnected_after_install": True,
                "skipped": True,
            }
            result = audit_stage(
                SetupStage.INSTALL_OR_SKIP,
                self.ctx.facts,
                plan=self.ctx.plan,
                stage_state=self.ctx.stage_state[SetupStage.INSTALL_OR_SKIP.value],
            )
            self._record_stage_result(result)
            return
        if self.ctx.facts:
            install_state = self.ctx.stage_state.setdefault(SetupStage.INSTALL_OR_SKIP.value, {})
            if self.ctx.facts.target_root_mounted:
                ok, failures, summary = self._run_pre_reboot_checks_with_retries()
                self.runlog.record(
                    "live_install_resume_precheck",
                    stage=SetupStage.INSTALL_OR_SKIP.value,
                    passed=ok,
                    failures=failures,
                    summary=summary,
                )
                if ok:
                    install_state["pre_reboot_checks_passed"] = True
                    self.bridge.add_message(
                        "agent",
                        "[harness] Existing live-install target already passes bootability "
                        "checks; skipping destructive disk/base install commands.",
                    )
                    self._ensure_reconnect_ssh_key()
                    self._raise_if_stop_requested(
                        stage=SetupStage.INSTALL_OR_SKIP,
                        reason="Agent stopped by user before controlled reboot.",
                    )
                    self._save_reboot_checkpoint(
                        stage=SetupStage.INSTALL_OR_SKIP,
                        reason="controlled install reboot after resume precheck",
                    )
                    self._raise_if_stop_requested(
                        stage=SetupStage.INSTALL_OR_SKIP,
                        reason="Agent stopped by user before controlled reboot.",
                    )
                    self._issue_install_reboot()
                    reconnect_user = self.ctx.created_user or self.runtime.user
                    reconnected, fresh_info, new_user = self.runtime.wait_for_reconnect(
                        reconnect_user
                    )
                    install_state["reconnected_after_install"] = reconnected
                    if not reconnected:
                        raise RuntimeError("Target did not reconnect after install reboot.")
                    if new_user:
                        self.runtime.user = new_user
                    self.ctx.system_info = fresh_info
                    self._refresh_facts(stage=SetupStage.INSTALL_OR_SKIP)
                    if self.ctx.facts and self.ctx.facts.is_live:
                        raise RuntimeError(
                            "Target reconnected but still appears to be the live environment."
                        )
                    result = audit_stage(
                        SetupStage.INSTALL_OR_SKIP,
                        self.ctx.facts or SetupFacts(),
                        plan=self.ctx.plan,
                        stage_state=install_state,
                    )
                    self._record_stage_result(result)
                    return
            self._ensure_live_install_target_user()
            installer = get_family_installer(self._family_adapter_key(self.ctx.facts))
            spine = installer.build_install_spine(self.ctx.facts, self.profile)
            install_state["family_adapter"] = {
                "family": spine.family,
                "adapter_version": spine.adapter_version,
                "command_count": len(spine.commands),
                "verification_count": len(spine.verification),
            }
            self.runlog.record(
                "family_adapter_spine",
                stage=SetupStage.INSTALL_OR_SKIP.value,
                adapter_name=spine.family,
                adapter_version=spine.adapter_version,
                commands=spine.commands,
                verification=spine.verification,
            )
            self._enforce_live_family_install_spine(spine.commands, spine.verification)
        self._run_stage_groups(SetupStage.INSTALL_OR_SKIP, audit_after=False)
        install_state = self.ctx.stage_state.setdefault(SetupStage.INSTALL_OR_SKIP.value, {})
        if install_state.get("installer_reboot_observed"):
            install_state["pre_reboot_checks_passed"] = True
            self.bridge.add_message(
                "agent",
                "Installer disconnected during live install; treating this as the install "
                "reboot and polling for the installed system.",
            )
        else:
            self._ensure_reconnect_ssh_key()
            ok, failures, summary = self._run_pre_reboot_checks_with_retries()
            install_state["pre_reboot_checks_passed"] = ok
            self.runlog.record(
                "pre_reboot_checks",
                passed=ok,
                failures=failures,
                summary=summary,
            )
            if not ok:
                raise RuntimeError(summary or "; ".join(failures))
            self._raise_if_stop_requested(
                stage=SetupStage.INSTALL_OR_SKIP,
                reason="Agent stopped by user before controlled reboot.",
            )
            self._save_reboot_checkpoint(
                stage=SetupStage.INSTALL_OR_SKIP,
                reason="controlled install reboot after pre-reboot checks",
            )
            self._raise_if_stop_requested(
                stage=SetupStage.INSTALL_OR_SKIP,
                reason="Agent stopped by user before controlled reboot.",
            )
            self._issue_install_reboot()
        reconnect_user = self.ctx.created_user or self.runtime.user
        reconnected, fresh_info, new_user = self.runtime.wait_for_reconnect(reconnect_user)
        self.ctx.stage_state.setdefault(SetupStage.INSTALL_OR_SKIP.value, {})[
            "reconnected_after_install"
        ] = reconnected
        if not reconnected:
            raise RuntimeError(fresh_info or "Target did not reconnect after install reboot.")
        if new_user:
            self.runtime.user = new_user
        self.ctx.system_info = fresh_info
        self._refresh_facts(stage=SetupStage.INSTALL_OR_SKIP)
        if self.ctx.facts and self.ctx.facts.is_live:
            raise RuntimeError(
                "Target reconnected, but it is still reporting a live environment after install reboot."
            )
        if self.ctx.facts is not None:
            self.ctx.facts = self.runtime.bootstrap_nopasswd_sudo(self.ctx.facts)
        result = audit_stage(
            SetupStage.INSTALL_OR_SKIP,
            self.ctx.facts,
            plan=self.ctx.plan,
            stage_state=self.ctx.stage_state[SetupStage.INSTALL_OR_SKIP.value],
        )
        self._record_stage_result(result)

    @staticmethod
    def _json_safe_checkpoint(value: Any) -> Any:
        return json.loads(json.dumps(value, default=str))

    def _checkpoint_pending_questions(self) -> list[dict[str, Any]]:
        questions: list[dict[str, Any]] = []
        for question in self.bridge.get_pending_questions():
            questions.append(
                {
                    "id": question.id,
                    "question": question.question,
                    "choices": list(question.choices or []),
                    "blocking": question.blocking,
                    "phase": question.phase,
                    "context": copy.deepcopy(question.context),
                }
            )
        return questions

    @staticmethod
    def _checkpoint_list_values(stage_state: dict[str, dict[str, Any]], key: str) -> list[Any]:
        values: list[Any] = []
        for state in stage_state.values():
            raw = state.get(key)
            if isinstance(raw, list):
                values.extend(copy.deepcopy(raw))
            elif raw:
                values.append(copy.deepcopy(raw))
        return values

    def _save_reboot_checkpoint(self, *, stage: SetupStage, reason: str) -> None:
        setup_snapshot = self.bridge.snapshot_setup_run()
        checkpoint: dict[str, Any] = {
            "run_id": self.ctx.run_id,
            "stage": stage.value,
            "reason": reason,
            "created_user": self.ctx.created_user,
            "runtime_user": self.runtime.user,
            "plan": asdict(self.ctx.plan) if self.ctx.plan else None,
            "stage_state": copy.deepcopy(self.ctx.stage_state),
            "stage_results": [asdict(result) for result in self.ctx.stage_results],
            "completed_groups": self._checkpoint_list_values(
                self.ctx.stage_state, "completed_groups"
            ),
            "manual_followups": self._checkpoint_list_values(
                self.ctx.stage_state, "manual_followups"
            ),
            "repair_attempts": {
                stage_name: copy.deepcopy(state.get("repair_attempts", {}))
                for stage_name, state in self.ctx.stage_state.items()
                if state.get("repair_attempts")
            },
            "attempt_loop_counts": {
                stage_name: copy.deepcopy(state.get("attempt_loop_counts", {}))
                for stage_name, state in self.ctx.stage_state.items()
                if state.get("attempt_loop_counts")
            },
            "pending_questions": self._checkpoint_pending_questions(),
            "active_question_id": setup_snapshot.get("active_question_id", ""),
            "waiting_for_input": setup_snapshot.get("waiting_for_input", False),
            "system_info_tail": (self.ctx.system_info or "")[-2000:],
            "results_log_tail": self.ctx.results_log[-10:],
        }
        if self.ctx.capability_store is not None:
            checkpoint["capability_store"] = self.ctx.capability_store.to_payload()
        checkpoint = self._json_safe_checkpoint(checkpoint)
        self.bridge.update_setup_run(
            stage=stage.value,
            status="running",
            reboot_checkpoint=checkpoint,
        )
        self.runlog.record(
            "setup_checkpoint_saved",
            stage=stage.value,
            reason=reason,
            checkpoint=checkpoint,
        )

    def _ensure_live_install_target_user(self) -> None:
        candidate = self._profile_target_username()
        if self._valid_target_username(candidate):
            self.profile.install_username = candidate
            self.ctx.created_user = candidate
            return
        for attempt in range(1, 4):
            answer = self._ask_blocking_question(
                (
                    "Which Linux username should QuickHatch create on the installed "
                    "system? Use lowercase letters, digits, `_` or `-`; do not use `root`."
                ),
                choices=None,
                stage=SetupStage.INSTALL_OR_SKIP,
                context={"target_username_required": True, "attempt": attempt},
                timeout=1800,
            )
            candidate = (answer or "").strip().lower()
            if self._valid_target_username(candidate):
                self.profile.install_username = candidate
                self.ctx.created_user = candidate
                self.runlog.record(
                    "target_user_confirmed",
                    stage=SetupStage.INSTALL_OR_SKIP.value,
                    username=candidate,
                )
                return
            self.bridge.add_message(
                "agent",
                "⚠️ Invalid Linux username. Expected lowercase letters, digits, `_` or `-`, "
                "starting with a letter or `_`, and not `root`.",
            )
        raise RuntimeError("Fresh install requires a valid target Linux username.")

    @staticmethod
    def _valid_target_username(candidate: str) -> bool:
        return bool(
            candidate and candidate != "root" and re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", candidate)
        )

    def _profile_target_username(self) -> str:
        try:
            family = self._family_adapter_key(self.ctx.facts)
            candidate = get_family_installer(family).target_user(self.profile)
        except Exception:
            candidate = ""
        candidate = (candidate or "").strip().lower()
        explicit = (getattr(self.profile, "install_username", "") or "").strip().lower()
        if self._valid_target_username(candidate) and (
            candidate not in _LIVE_SESSION_USER_NAMES
            and (candidate != "quickhatch" or self._valid_target_username(explicit))
        ):
            return candidate
        return ""

    def _run_pre_reboot_checks_with_retries(self) -> tuple[bool, list[str], str]:
        last_result: tuple[bool, list[str], str] = (False, [], "")
        max_attempts = 8
        for attempt in range(1, max_attempts + 1):
            ok, failures, summary = self.runtime.run_pre_reboot_checks()
            last_result = (ok, failures, summary)
            self.runlog.record(
                "pre_reboot_check_attempt",
                stage=SetupStage.INSTALL_OR_SKIP.value,
                attempt=attempt,
                passed=ok,
                failures=failures,
                summary=summary,
            )
            self._raise_if_stop_requested(stage=SetupStage.INSTALL_OR_SKIP)
            if ok:
                return last_result
            if self._try_repair_live_fedora_kernel_artifacts(failures, summary):
                continue
            if not self._looks_like_transient_ssh_disconnect(summary, *failures):
                return last_result
            if attempt >= max_attempts:
                return last_result
            self._raise_if_stop_requested(stage=SetupStage.INSTALL_OR_SKIP)
            delay = min(3 * attempt, 20)
            self.bridge.add_message(
                "agent",
                "Pre-reboot safety check hit a transient SSH disconnect; "
                f"retrying ({attempt + 1}/{max_attempts}) after {delay}s…",
            )
            time.sleep(delay)
        return last_result

    def _try_repair_live_fedora_kernel_artifacts(self, failures: list[str], summary: str) -> bool:
        facts = self.ctx.facts or SetupFacts()
        if not facts.is_live or (facts.distro_family != "fedora" and facts.os_id != "fedora"):
            return False
        stage_state = self.ctx.stage_state.setdefault(SetupStage.INSTALL_OR_SKIP.value, {})
        if stage_state.get("fedora_kernel_artifact_repair_attempted"):
            return False
        text = f"{summary}\n" + "\n".join(failures or [])
        lowered = text.lower()
        if "missing kernel or initramfs" not in lowered and "vmlinuz" not in lowered:
            return False

        stage_state["fedora_kernel_artifact_repair_attempted"] = True
        if not self._internal_capability_allows(
            kind="bootloader_install",
            target="/boot",
            stage=SetupStage.INSTALL_OR_SKIP,
            group_id="fedora-live-kernel-artifact-repair",
            reason="Fedora live boot artifact repair",
        ):
            return False
        command = self._fedora_live_kernel_artifact_repair_command("/mnt")
        self.bridge.add_message(
            "agent",
            "Fedora installroot is missing boot kernel artifacts; repairing `/mnt/boot` before reboot checks retry.",
        )
        self.bridge.add_message("agent", f"`{redact_sensitive_text(command)}`")
        rc, stdout, stderr = self.runtime.ssh_exec(command, 300)
        self.ctx.results_log.append(
            redact_sensitive_text(f"$ {command}\n[rc={rc}]\n{stdout}\n{stderr}".strip())
        )
        self.runlog.record(
            "fedora_live_kernel_artifact_repair",
            stage=SetupStage.INSTALL_OR_SKIP.value,
            command=command,
            rc=rc,
            stdout_tail=(stdout or "")[-1000:],
            stderr_tail=(stderr or "")[-1000:],
        )
        stage_state["fedora_kernel_artifact_repair_rc"] = rc
        if rc == 0:
            self.bridge.add_message(
                "agent",
                "✓ Fedora boot kernel artifacts repaired; retrying pre-reboot safety checks.",
            )
            return True
        detail = (stderr or stdout or "").strip()
        self.bridge.add_message(
            "agent",
            f"⚠️ Fedora boot artifact repair failed: {detail[:240]}",
        )
        return False

    @staticmethod
    def _fedora_live_kernel_artifact_repair_command(root_path: str) -> str:
        root_q = shlex.quote(root_path or "/mnt")
        return (
            f"set -e; qh_root={root_q}; "
            'qh_sudo=sudo; [ "$(id -u)" -eq 0 ] && qh_sudo=; '
            'test -d "$qh_root/etc"; '
            'if ! grep -qiE "^ID(_LIKE)?=.*fedora" "$qh_root/etc/os-release"; then '
            'echo "target is not Fedora"; exit 2; fi; '
            "for qh_bind in dev dev/pts proc sys run; do "
            '$qh_sudo mkdir -p "$qh_root/$qh_bind"; '
            'mountpoint -q "$qh_root/$qh_bind" || $qh_sudo mount --bind "/$qh_bind" "$qh_root/$qh_bind" || true; '
            "done; "
            'qh_kver="$($qh_sudo chroot "$qh_root" sh -c \'ls /usr/lib/modules 2>/dev/null | sort -V | tail -n1\')"; '
            'test -n "$qh_kver"; '
            'test -f "$qh_root/usr/lib/modules/$qh_kver/vmlinuz"; '
            '$qh_sudo mkdir -p "$qh_root/boot"; '
            'if ! test -s "$qh_root/boot/vmlinuz-$qh_kver"; then '
            '$qh_sudo cp "$qh_root/usr/lib/modules/$qh_kver/vmlinuz" "$qh_root/boot/vmlinuz-$qh_kver"; '
            "fi; "
            'if ! test -s "$qh_root/boot/initramfs-$qh_kver.img"; then '
            '$qh_sudo chroot "$qh_root" dracut --force --kver "$qh_kver" "/boot/initramfs-$qh_kver.img"; '
            "fi; "
            "if $qh_sudo chroot \"$qh_root\" sh -c 'command -v grub2-mkconfig >/dev/null 2>&1'; then "
            "$qh_sudo chroot \"$qh_root\" sh -c 'mkdir -p /boot/grub2 && grub2-mkconfig -o /boot/grub2/grub.cfg'; "
            "fi; "
            'test -s "$qh_root/boot/vmlinuz-$qh_kver"; '
            'test -s "$qh_root/boot/initramfs-$qh_kver.img"'
        )

    def _ensure_reconnect_ssh_key(self) -> None:
        root_path = "/mnt"
        reconnect_user = self.ctx.created_user or self.runtime.user
        if self.ctx.facts and self.ctx.facts.is_live:
            root_path, reconnect_user = self._ensure_live_install_reconnect_user()
        if not reconnect_user or reconnect_user == "root":
            return
        public_key = self._resolve_public_key_text()
        if not public_key:
            raise RuntimeError(
                "Cannot verify SSH continuity before reboot: QuickHatch public key is missing."
            )

        root_q = shlex.quote(root_path)
        user_q = shlex.quote(reconnect_user)
        key_q = shlex.quote(public_key)
        command = (
            f"set -e; "
            f"qh_root={root_q}; "
            f"qh_user={user_q}; "
            f'qh_sudo=sudo; [ "$(id -u)" -eq 0 ] && qh_sudo=; '
            f'qh_home="$qh_root/home/$qh_user"; '
            f'test -d "$qh_home"; '
            f"qh_owner=$(stat -c '%u:%g' \"$qh_home\"); "
            f'$qh_sudo install -d -m 700 "$qh_home/.ssh"; '
            f'$qh_sudo touch "$qh_home/.ssh/authorized_keys"; '
            f'$qh_sudo grep -qxF {key_q} "$qh_home/.ssh/authorized_keys" '
            f"|| printf '%s\\n' {key_q} | "
            f'$qh_sudo tee -a "$qh_home/.ssh/authorized_keys" >/dev/null; '
            f'$qh_sudo chown "$qh_owner" "$qh_home/.ssh" "$qh_home/.ssh/authorized_keys"; '
            f'$qh_sudo chmod 700 "$qh_home/.ssh"; '
            f'$qh_sudo chmod 600 "$qh_home/.ssh/authorized_keys"; '
            f'$qh_sudo grep -qxF {key_q} "$qh_home/.ssh/authorized_keys"'
        )
        self.bridge.add_message(
            "agent",
            f"`{redact_sensitive_text(command)}` — ensure SSH reconnect key",
        )
        last_output = ""
        success = False
        max_attempts = 8
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                delay = min(2 * attempt, 15)
                self.bridge.add_message(
                    "agent",
                    "Retrying SSH reconnect key verification "
                    f"({attempt}/{max_attempts}) after {delay}s…",
                )
                time.sleep(delay)
            rc, stdout, stderr = self.runtime.ssh_exec(command, 60)
            last_output = (stderr or stdout or "").strip()
            self.ctx.results_log.append(
                redact_sensitive_text(f"$ {command}\n[rc={rc}]\n{stdout}\n{stderr}".strip())
            )
            self.ctx.results_log[:] = self.ctx.results_log[-self.runtime.results_log_limit :]
            self.runlog.record(
                "ssh_continuity_key",
                stage=SetupStage.INSTALL_OR_SKIP.value,
                user=reconnect_user,
                attempt=attempt,
                rc=rc,
                stdout_tail=(stdout or "")[-500:],
                stderr_tail=(stderr or "")[-500:],
            )
            if rc == 0:
                success = True
                break
            if self._verify_reconnect_ssh_key(reconnect_user, public_key, root_path=root_path):
                self.bridge.add_message(
                    "agent",
                    "SSH reconnect key is present despite a transient SSH command failure.",
                )
                success = True
                break
            if attempt >= 3 and not self._looks_like_transient_ssh_disconnect(
                stdout, stderr, last_output
            ):
                break
        if not success:
            raise RuntimeError(
                "Cannot verify SSH continuity before reboot: failed to install "
                f"QuickHatch public key for '{reconnect_user}'. "
                f"{last_output[:240]}"
            )
        if self.ctx.facts and self.ctx.facts.is_live:
            self._ensure_live_target_network_reconnect(root_path)
            self._ensure_live_target_uefi_boot_fallback(root_path)
        self.ctx.stage_state.setdefault(SetupStage.INSTALL_OR_SKIP.value, {})[
            "ssh_continuity_key_installed"
        ] = True

    def _ensure_live_target_network_reconnect(self, root_path: str) -> None:
        command = self._live_target_network_reconnect_command(root_path)
        self.bridge.add_message(
            "agent",
            "`" + redact_sensitive_text(command) + "` — ensure post-reboot network reconnect",
        )
        rc, stdout, stderr = self.runtime.ssh_exec(command, 60)
        self.ctx.results_log.append(
            redact_sensitive_text(f"$ {command}\n[rc={rc}]\n{stdout}\n{stderr}".strip())
        )
        self.ctx.results_log[:] = self.ctx.results_log[-self.runtime.results_log_limit :]
        self.runlog.record(
            "live_target_network_reconnect",
            stage=SetupStage.INSTALL_OR_SKIP.value,
            root_path=root_path,
            rc=rc,
            stdout_tail=(stdout or "")[-500:],
            stderr_tail=(stderr or "")[-500:],
        )
        if rc != 0:
            detail = (stderr or stdout or "").strip()
            raise RuntimeError(
                "Cannot verify SSH continuity before reboot: failed to write installed "
                f"network reconnect config. {detail[:240]}"
            )

    def _ensure_live_target_uefi_boot_fallback(self, root_path: str) -> None:
        if not self._internal_capability_allows(
            kind="bootloader_install",
            target="/boot",
            stage=SetupStage.INSTALL_OR_SKIP,
            group_id="live-target-uefi-boot-fallback",
            reason="removable UEFI boot fallback",
        ):
            raise RuntimeError(
                "Cannot verify boot continuity before reboot: removable UEFI boot fallback "
                "requires a confirmed bootloader capability."
            )
        command = self._live_target_uefi_boot_fallback_command(root_path)
        self.bridge.add_message(
            "agent",
            "`" + redact_sensitive_text(command) + "` — ensure removable UEFI boot fallback",
        )
        rc, stdout, stderr = self.runtime.ssh_exec(command, 180)
        self.ctx.results_log.append(
            redact_sensitive_text(f"$ {command}\n[rc={rc}]\n{stdout}\n{stderr}".strip())
        )
        self.ctx.results_log[:] = self.ctx.results_log[-self.runtime.results_log_limit :]
        self.runlog.record(
            "live_target_uefi_boot_fallback",
            stage=SetupStage.INSTALL_OR_SKIP.value,
            root_path=root_path,
            rc=rc,
            stdout_tail=(stdout or "")[-500:],
            stderr_tail=(stderr or "")[-500:],
        )
        if rc != 0:
            detail = (stderr or stdout or "").strip()
            raise RuntimeError(
                "Cannot verify boot continuity before reboot: failed to install a "
                f"removable UEFI boot fallback. {detail[:240]}"
            )

    @staticmethod
    def _live_target_uefi_boot_fallback_command(root_path: str) -> str:
        root_q = shlex.quote(root_path)
        return (
            f"set -e; qh_root={root_q}; "
            f'qh_sudo=sudo; [ "$(id -u)" -eq 0 ] && qh_sudo=; '
            f"[ -d /sys/firmware/efi ] || exit 0; "
            f'test -d "$qh_root/etc"; '
            f"for qh_bind in dev dev/pts proc sys run; do "
            f'$qh_sudo mkdir -p "$qh_root/$qh_bind"; '
            f'mountpoint -q "$qh_root/$qh_bind" '
            f'|| $qh_sudo mount --bind "/$qh_bind" "$qh_root/$qh_bind" '
            f"|| true; "
            f"done; "
            f'$qh_sudo chroot "$qh_root" sh -c '
            f"'command -v grub-install >/dev/null 2>&1' "
            f"|| exit 0; "
            f'qh_efi_dir=""; '
            f"for qh_dir in /boot/efi /boot; do "
            f'if mountpoint -q "$qh_root$qh_dir" '
            f'|| grep -Eq "[[:space:]]$qh_dir[[:space:]]+vfat" "$qh_root/etc/fstab" '
            f'|| [ -d "$qh_root$qh_dir/EFI" ]; then '
            f'qh_efi_dir="$qh_dir"; break; fi; '
            f"done; "
            f'[ -n "$qh_efi_dir" ] || exit 0; '
            f'$qh_sudo chroot "$qh_root" grub-install --target=x86_64-efi '
            f'--efi-directory="$qh_efi_dir" --bootloader-id=quickhatch '
            f"--recheck || true; "
            f'if ! test -f "$qh_root$qh_efi_dir/EFI/BOOT/BOOTX64.EFI" '
            f'&& ! test -f "$qh_root$qh_efi_dir/EFI/boot/bootx64.efi"; then '
            f'$qh_sudo chroot "$qh_root" grub-install --target=x86_64-efi '
            f'--efi-directory="$qh_efi_dir" --bootloader-id=quickhatch '
            f"--removable --no-nvram --recheck; "
            f"fi; "
            f"if $qh_sudo chroot \"$qh_root\" sh -c 'command -v update-grub >/dev/null 2>&1'; then "
            f'$qh_sudo chroot "$qh_root" update-grub; '
            f"elif $qh_sudo chroot \"$qh_root\" sh -c 'command -v grub-mkconfig >/dev/null 2>&1'; then "
            f'$qh_sudo chroot "$qh_root" grub-mkconfig -o /boot/grub/grub.cfg; '
            f"fi; "
            f"if $qh_sudo chroot \"$qh_root\" sh -c 'command -v grub-editenv >/dev/null 2>&1'; then "
            f'$qh_sudo chroot "$qh_root" sh -c '
            f"'test -f /boot/grub/grubenv && grub-editenv /boot/grub/grubenv list >/dev/null 2>&1 "
            f"|| grub-editenv /boot/grub/grubenv create' || true; "
            f"fi; "
            f'test -f "$qh_root$qh_efi_dir/EFI/BOOT/BOOTX64.EFI" '
            f'|| test -f "$qh_root$qh_efi_dir/EFI/boot/bootx64.efi"; '
            f"if command -v efibootmgr >/dev/null 2>&1; then "
            f'qh_efi_source="$(findmnt -n -o SOURCE --target "$qh_root$qh_efi_dir" '
            f'2>/dev/null | head -n1)"; '
            f'qh_efi_disk=""; qh_efi_part=""; qh_efi_partuuid_lc=""; '
            f'if [ -n "$qh_efi_source" ]; then '
            f'qh_efi_pk="$(lsblk -no PKNAME "$qh_efi_source" 2>/dev/null | head -n1)"; '
            f'qh_efi_part="$(lsblk -no PARTN "$qh_efi_source" 2>/dev/null | head -n1)"; '
            f'qh_efi_partuuid_lc="$(blkid -s PARTUUID -o value "$qh_efi_source" '
            f'2>/dev/null | tr "A-F" "a-f")"; '
            f'[ -n "$qh_efi_pk" ] && qh_efi_disk="/dev/$qh_efi_pk"; '
            f"fi; "
            f'qh_boot_entry="$($qh_sudo efibootmgr -v 2>/dev/null | '
            f'awk -v p="$qh_efi_partuuid_lc" '
            f"'/^Boot[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]\\*/ "
            f"{{line=tolower($0); "
            f"if (line !~ /(cd-rom|dvd|usb|network|pxe|uiapp|windows boot manager)/ "
            f"&& line ~ /(quickhatch|ubuntu|debian|fedora|arch|opensuse|grub|linux)/ "
            f'&& (p == "" || index(line, p) > 0)) '
            f"{{print substr($1,5,4); exit}}}}')\"; "
            f'if [ -z "$qh_boot_entry" ]; then '
            f'qh_efi_loader=""; '
            f'if test -f "$qh_root$qh_efi_dir/EFI/quickhatch/grubx64.efi"; then '
            f"qh_efi_loader='\\EFI\\quickhatch\\grubx64.efi'; "
            f'elif test -f "$qh_root$qh_efi_dir/EFI/ubuntu/grubx64.efi"; then '
            f"qh_efi_loader='\\EFI\\ubuntu\\grubx64.efi'; "
            f'elif test -f "$qh_root$qh_efi_dir/EFI/debian/grubx64.efi"; then '
            f"qh_efi_loader='\\EFI\\debian\\grubx64.efi'; "
            f'elif test -f "$qh_root$qh_efi_dir/EFI/fedora/grubx64.efi"; then '
            f"qh_efi_loader='\\EFI\\fedora\\grubx64.efi'; "
            f"else "
            f"qh_efi_loader='\\EFI\\BOOT\\BOOTX64.EFI'; "
            f"fi; "
            f'if [ -n "$qh_efi_disk" ] && [ -n "$qh_efi_part" ]; then '
            f'$qh_sudo efibootmgr -c -d "$qh_efi_disk" -p "$qh_efi_part" '
            f'-L quickhatch -l "$qh_efi_loader" || true; '
            f"fi; "
            f'qh_boot_entry="$($qh_sudo efibootmgr -v 2>/dev/null | '
            f'awk -v p="$qh_efi_partuuid_lc" '
            f"'/^Boot[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]\\*/ "
            f"{{line=tolower($0); "
            f"if (line !~ /(cd-rom|dvd|usb|network|pxe|uiapp|windows boot manager)/ "
            f"&& line ~ /(quickhatch|ubuntu|debian|fedora|arch|opensuse|grub|linux)/ "
            f'&& (p == "" || index(line, p) > 0)) '
            f"{{print substr($1,5,4); exit}}}}')\"; "
            f"fi; "
            f'if [ -z "$qh_boot_entry" ]; then '
            f'qh_boot_entry="$($qh_sudo efibootmgr -v 2>/dev/null | '
            f"awk '/^Boot[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]\\*/ "
            f"{{line=tolower($0); "
            f"if (line !~ /(cd-rom|dvd|usb|network|pxe|uiapp|windows boot manager)/ "
            f"&& line ~ /(vbox harddisk|qemu harddisk|uefi os)/) "
            f"{{print substr($1,5,4); exit}}}}')\"; "
            f"fi; "
            f'[ -n "$qh_boot_entry" ] '
            f'&& $qh_sudo efibootmgr -n "$qh_boot_entry" || true; '
            f'qh_boot_order="$($qh_sudo efibootmgr 2>/dev/null | '
            f"awk -F': ' '/^BootOrder:/ {{print $2; exit}}')\"; "
            f'if [ -n "$qh_boot_entry" ] && [ -n "$qh_boot_order" ]; then '
            f'qh_boot_rest="$(printf "%s\\n" "$qh_boot_order" | tr "," "\\n" '
            f'| awk -v b="$qh_boot_entry" \'$0 != b && $0 != ""\' '
            f'| paste -sd, -)"; '
            f'if [ -n "$qh_boot_rest" ]; then '
            f'qh_new_order="$qh_boot_entry,$qh_boot_rest"; '
            f'else qh_new_order="$qh_boot_entry"; fi; '
            f'$qh_sudo efibootmgr -o "$qh_new_order" || true; '
            f"fi; "
            f'[ -n "$qh_boot_entry" ] '
            f'&& $qh_sudo efibootmgr | sed -n "1,4p" || true; '
            f"fi"
        )

    @staticmethod
    def _live_target_network_reconnect_command(root_path: str) -> str:
        root_q = shlex.quote(root_path)
        nm_payload = (
            "[connection]\n"
            "id=quickhatch-wired-dhcp\n"
            "type=ethernet\n"
            "autoconnect=true\n"
            "\n"
            "[ethernet]\n"
            "\n"
            "[ipv4]\n"
            "method=auto\n"
            "\n"
            "[ipv6]\n"
            "method=auto\n"
        )
        netplan_payload = "network:\n  version: 2\n  renderer: NetworkManager\n"
        managed_payload = "[keyfile]\nunmanaged-devices=none\n"
        return (
            f"set -e; qh_root={root_q}; "
            f'qh_sudo=sudo; [ "$(id -u)" -eq 0 ] && qh_sudo=; '
            f'test -d "$qh_root/etc"; '
            f'$qh_sudo mkdir -p "$qh_root/etc/NetworkManager/system-connections"; '
            f"printf '%s\\n' {shlex.quote(nm_payload)} | "
            f'$qh_sudo tee "$qh_root/etc/NetworkManager/system-connections/'
            f'quickhatch-wired-dhcp.nmconnection" >/dev/null; '
            f'$qh_sudo chmod 600 "$qh_root/etc/NetworkManager/system-connections/'
            f'quickhatch-wired-dhcp.nmconnection"; '
            f'$qh_sudo mkdir -p "$qh_root/etc/NetworkManager/conf.d"; '
            f"printf '%s\\n' {shlex.quote(managed_payload)} | "
            f'$qh_sudo tee "$qh_root/etc/NetworkManager/conf.d/10-quickhatch-managed.conf" '
            f">/dev/null; "
            f'if [ -d "$qh_root/etc/netplan" ]; then '
            f"printf '%s\\n' {shlex.quote(netplan_payload)} | "
            f'$qh_sudo tee "$qh_root/etc/netplan/01-quickhatch-networkmanager.yaml" '
            f">/dev/null; "
            f'$qh_sudo chmod 600 "$qh_root/etc/netplan/01-quickhatch-networkmanager.yaml"; fi; '
            f'$qh_sudo chroot "$qh_root" systemctl enable NetworkManager '
            f">/dev/null 2>&1 || true; "
            f'$qh_sudo chroot "$qh_root" systemctl enable ssh >/dev/null 2>&1 '
            f'|| $qh_sudo chroot "$qh_root" systemctl enable sshd >/dev/null 2>&1 '
            f"|| true"
        )

    def _ensure_live_install_reconnect_user(self) -> tuple[str, str]:
        if (
            self._valid_target_username(self.ctx.created_user or "")
            and self.ctx.created_user not in _LIVE_SESSION_USER_NAMES
        ):
            root_path = self._detect_live_target_root_path()
            target_user = str(self.ctx.created_user)
            self._create_live_target_reconnect_user(root_path, target_user)
            self.ctx.stage_state.setdefault(SetupStage.INSTALL_OR_SKIP.value, {})[
                "target_reconnect_user"
            ] = target_user
            return root_path, target_user

        root_path = self._detect_live_target_root_path()
        target_user = self._profile_target_username()
        if target_user:
            self._create_live_target_reconnect_user(root_path, target_user)
            self.ctx.created_user = target_user
            self.ctx.stage_state.setdefault(SetupStage.INSTALL_OR_SKIP.value, {})[
                "target_reconnect_user"
            ] = target_user
            return root_path, target_user

        existing_user = self._detect_live_target_admin_user(root_path)
        if existing_user:
            self._create_live_target_reconnect_user(root_path, existing_user)
            self.ctx.created_user = existing_user
            self.ctx.stage_state.setdefault(SetupStage.INSTALL_OR_SKIP.value, {})[
                "target_reconnect_user"
            ] = existing_user
            return root_path, existing_user

        candidates = self._planned_target_user_candidates()
        if not candidates and self.runtime.user == "root":
            return root_path, "root"

        if not candidates:
            raise RuntimeError(
                "Cannot verify SSH continuity before reboot: no sudo-capable installed "
                "user exists, and the research plan did not name a target user to create."
            )
        target_user = candidates[0]
        self._create_live_target_reconnect_user(root_path, target_user)
        self.ctx.created_user = target_user
        state = self.ctx.stage_state.setdefault(SetupStage.INSTALL_OR_SKIP.value, {})
        state["target_reconnect_user"] = target_user
        state["target_user_harness_created"] = True
        return root_path, target_user

    def _detect_live_target_root_path(self) -> str:
        command = (
            "for qh_root in "
            + " ".join(shlex.quote(path) for path in _LIVE_TARGET_ROOT_CANDIDATES)
            + '; do test -f "$qh_root/etc/passwd" && printf "%s\\n" "$qh_root" && exit 0; '
            "done; exit 1"
        )
        rc, stdout, stderr = self.runtime.ssh_exec(command, 30)
        self.runlog.record(
            "live_target_root_detect",
            stage=SetupStage.INSTALL_OR_SKIP.value,
            rc=rc,
            stdout_tail=(stdout or "")[-500:],
            stderr_tail=(stderr or "")[-500:],
        )
        if rc == 0:
            for line in (stdout or "").splitlines():
                candidate = line.strip()
                if candidate.startswith("/"):
                    return candidate
        raise RuntimeError(
            "Cannot verify SSH continuity before reboot: no mounted target root was "
            "found at /mnt, /target, /mnt/sysroot, or /mnt/sysimage."
        )

    def _detect_live_target_admin_user(self, root_path: str) -> str:
        root_q = shlex.quote(root_path)
        command = (
            f"qh_root={root_q}; "
            f'qh_sudo=sudo; [ "$(id -u)" -eq 0 ] && qh_sudo=; '
            f'test -f "$qh_root/etc/passwd" || exit 1; '
            f"qh_groups=$($qh_sudo awk -F: "
            f'\'$1=="sudo" || $1=="wheel" || $1=="admin" {{print "," $4 ","}}\' '
            f'"$qh_root/etc/group" | tr -d "\\n"); '
            f'$qh_sudo awk -F: -v groups="$qh_groups" '
            f"'$3 >= 1000 && $3 < 60000 && $7 !~ /(nologin|false)$/ "
            f'&& index(groups, "," $1 ",") > 0 {{print $1; found=1; exit}} '
            f'END {{exit !found}}\' "$qh_root/etc/passwd"'
        )
        rc, stdout, stderr = self.runtime.ssh_exec(command, 30)
        self.runlog.record(
            "live_target_admin_user_detect",
            stage=SetupStage.INSTALL_OR_SKIP.value,
            root_path=root_path,
            rc=rc,
            stdout_tail=(stdout or "")[-500:],
            stderr_tail=(stderr or "")[-500:],
        )
        if rc != 0:
            return ""
        for line in (stdout or "").splitlines():
            user = line.strip()
            if self._valid_target_username(user):
                return user
        return ""

    def _planned_target_user_candidates(self) -> list[str]:
        candidates: list[str] = []

        def add(user: str | None) -> None:
            user = (user or "").strip()
            if (
                self._valid_target_username(user)
                and user not in _LIVE_SESSION_USER_NAMES
                and user not in candidates
            ):
                candidates.append(user)

        add(self.ctx.created_user)
        add(getattr(self.runtime.profile, "install_username", ""))
        if self.ctx.plan:
            for group in self.ctx.plan.command_groups:
                if group.stage != SetupStage.INSTALL_OR_SKIP:
                    continue
                for command in group.commands:
                    add(self._extract_created_user(command))
        runtime_user = (getattr(self.runtime.profile, "linux_user", "") or "").strip()
        add(runtime_user)
        return candidates

    @staticmethod
    def _valid_target_username(user: str) -> bool:
        return bool(user and user != "root" and re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", user or ""))

    def _create_live_target_reconnect_user(self, root_path: str, target_user: str) -> None:
        root_q = shlex.quote(root_path)
        user_q = shlex.quote(target_user)
        password = (
            getattr(self.runtime.profile, "default_account_password", "")
            or generate_setup_password()
        )
        password_q = shlex.quote(password)
        sudoers_line_q = shlex.quote(f"{target_user} ALL=(ALL:ALL) NOPASSWD: ALL")
        command = (
            f"set -e; "
            f"qh_root={root_q}; "
            f"qh_user={user_q}; "
            f"qh_pw={password_q}; "
            f'qh_sudo=sudo; [ "$(id -u)" -eq 0 ] && qh_sudo=; '
            f'test -f "$qh_root/etc/passwd"; '
            f'if $qh_sudo chroot "$qh_root" getent group sudo >/dev/null 2>&1; then '
            f"qh_group=sudo; "
            f'elif $qh_sudo chroot "$qh_root" getent group wheel >/dev/null 2>&1; then '
            f"qh_group=wheel; "
            f'else $qh_sudo chroot "$qh_root" groupadd sudo; qh_group=sudo; fi; '
            f'if $qh_sudo chroot "$qh_root" id -u "$qh_user" >/dev/null 2>&1; then '
            f'$qh_sudo chroot "$qh_root" usermod -aG "$qh_group" "$qh_user"; '
            f"else "
            f'$qh_sudo chroot "$qh_root" useradd -m -G "$qh_group" -s /bin/bash "$qh_user"; '
            f"fi; "
            f'printf "%s:%s\\n" "$qh_user" "$qh_pw" | $qh_sudo chroot "$qh_root" chpasswd; '
            f'printf "root:%s\\n" "$qh_pw" | $qh_sudo chroot "$qh_root" chpasswd || true; '
            f'$qh_sudo chroot "$qh_root" install -d -m 755 /etc/sudoers.d; '
            f"printf '%s\\n' {sudoers_line_q} | "
            f'$qh_sudo chroot "$qh_root" tee /etc/sudoers.d/99-quickhatch-nopasswd '
            f">/dev/null; "
            f'$qh_sudo chroot "$qh_root" chmod 440 '
            f"/etc/sudoers.d/99-quickhatch-nopasswd; "
            f'$qh_sudo chroot "$qh_root" grep -qxF {sudoers_line_q} '
            f"/etc/sudoers.d/99-quickhatch-nopasswd; "
            f'$qh_sudo chroot "$qh_root" install -d -m 700 -o "$qh_user" -g "$qh_user" '
            f'"/home/$qh_user/.ssh"; '
            f'test -d "$qh_root/home/$qh_user"'
        )
        self.bridge.add_message(
            "agent",
            f"`{redact_sensitive_text(command)}` — ensure installed sudo user for reconnect",
        )
        rc, stdout, stderr = self.runtime.ssh_exec(command, 60)
        self.ctx.results_log.append(
            redact_sensitive_text(f"$ {command}\n[rc={rc}]\n{stdout}\n{stderr}".strip())
        )
        self.ctx.results_log[:] = self.ctx.results_log[-self.runtime.results_log_limit :]
        self.runlog.record(
            "live_target_user_ensure",
            stage=SetupStage.INSTALL_OR_SKIP.value,
            root_path=root_path,
            user=target_user,
            rc=rc,
            stdout_tail=(stdout or "")[-500:],
            stderr_tail=(stderr or "")[-500:],
        )
        if rc != 0:
            detail = (stderr or stdout or "").strip()
            raise RuntimeError(
                "Cannot verify SSH continuity before reboot: failed to create or repair "
                f"installed user '{target_user}'. {detail[:240]}"
            )

    def _verify_reconnect_ssh_key(
        self, reconnect_user: str, public_key: str, *, root_path: str = "/mnt"
    ) -> bool:
        root_q = shlex.quote(root_path)
        user_q = shlex.quote(reconnect_user)
        key_q = shlex.quote(public_key)
        command = (
            f"qh_root={root_q}; "
            f"qh_user={user_q}; "
            f'qh_sudo=sudo; [ "$(id -u)" -eq 0 ] && qh_sudo=; '
            f'qh_home="$qh_root/home/$qh_user"; '
            f'test -d "$qh_home" && '
            f'test -f "$qh_home/.ssh/authorized_keys" && '
            f'$qh_sudo grep -qxF {key_q} "$qh_home/.ssh/authorized_keys"'
        )
        rc, stdout, stderr = self.runtime.ssh_exec(command, 30)
        self.runlog.record(
            "ssh_continuity_key_verify",
            stage=SetupStage.INSTALL_OR_SKIP.value,
            user=reconnect_user,
            rc=rc,
            stdout_tail=(stdout or "")[-500:],
            stderr_tail=(stderr or "")[-500:],
        )
        return rc == 0

    @staticmethod
    def _looks_like_transient_ssh_disconnect(*texts: str) -> bool:
        text = "\n".join(str(item or "") for item in texts).lower()
        return any(
            marker in text
            for marker in (
                "rc=255",
                "connection closed",
                "closed by remote host",
                "connection reset",
                "kex_exchange_identification",
                "connection timed out",
            )
        )

    def _looks_like_live_installer_reboot_disconnect(
        self, stage: SetupStage, command: str, stdout: str, stderr: str
    ) -> bool:
        facts = self.ctx.facts or SetupFacts()
        if stage != SetupStage.INSTALL_OR_SKIP or not facts.is_live:
            return False
        command_text = (command or "").lower()
        if not any(
            token in command_text
            for token in (
                "anaconda",
                "subiquity",
                "ubuntu-desktop-bootstrap",
                "curtin",
                "calamares",
            )
        ):
            return False
        return self._looks_like_transient_ssh_disconnect(stdout, stderr)

    def _looks_like_live_disk_reread_required(
        self, stage: SetupStage, command: str, stdout: str, stderr: str
    ) -> bool:
        facts = self.ctx.facts or SetupFacts()
        if stage != SetupStage.INSTALL_OR_SKIP or not facts.is_live:
            return False
        if not _LIVE_DISK_REREAD_COMMAND_RE.search(command or ""):
            return False
        output = "\n".join([stdout or "", stderr or ""])
        return bool(_LIVE_DISK_REREAD_REQUIRED_RE.search(output))

    @staticmethod
    def _live_disk_reread_required_message(command: str) -> str:
        return (
            "Live install disk state is stale: the kernel reports that the target "
            "disk partition table could not be reread or is still busy after "
            f"`{redact_sensitive_text(command)}`. QuickHatch stopped instead of "
            "escalating destructive disk commands. Reboot back into the same live "
            "USB/ISO, rerun quickstart.sh, then restart Remote Setup from a clean "
            "live session."
        )

    def _resolve_public_key_text(self) -> str:
        candidates: list[Path] = []
        raw_profile_path = getattr(self.runtime.profile, "ssh_public_key_path", "") or ""
        if raw_profile_path:
            candidates.append(Path(raw_profile_path))
        if self.runtime.key_path:
            candidates.append(Path(self.runtime.key_path).with_suffix(".pub"))
        candidates.append(Path.home() / ".ssh" / "quickhatch_ed25519.pub")

        for candidate in candidates:
            try:
                text = candidate.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if text.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")):
                return text
        return ""

    def _issue_install_reboot(self) -> None:
        reboot_inner = (
            "sleep 1; "
            "if command -v systemctl >/dev/null 2>&1; then "
            "systemctl reboot --force --force || reboot -f || reboot; "
            "else reboot -f || reboot; fi"
        )
        background_reboot = (
            "nohup sh -c "
            + shlex.quote(reboot_inner)
            + " >/tmp/quickhatch-reboot.log 2>&1 </dev/null &"
        )
        command = (
            'qh_sudo=; [ "$(id -u)" -eq 0 ] || qh_sudo=sudo; '
            "$qh_sudo umount -R /mnt 2>/dev/null || true; "
            "$qh_sudo umount -R /target 2>/dev/null || true; "
            "$qh_sudo sh -c " + shlex.quote(background_reboot)
        )
        self.bridge.add_message("agent", "Rebooting into the installed system…")
        self.runlog.record(
            "install_reboot",
            stage=SetupStage.INSTALL_OR_SKIP.value,
            command=command,
        )
        rc, stdout, stderr = self.runtime.ssh_exec(command, 30)
        self.runlog.record(
            "install_reboot_result",
            stage=SetupStage.INSTALL_OR_SKIP.value,
            rc=rc,
            stdout_tail=(stdout or "")[-500:],
            stderr_tail=(stderr or "")[-500:],
        )
        if rc != 0 and not self._looks_like_transient_ssh_disconnect(stdout, stderr):
            detail = (stderr or stdout or "reboot command failed").strip()
            raise RuntimeError(f"Install reboot command failed: {detail[:240]}")

    def _run_postboot_driver(self) -> None:
        if self.ctx.facts is not None:
            self._refresh_facts(stage=SetupStage.POSTBOOT_DRIVER)
        if self.ctx.facts is not None and not self.ctx.facts.fact_probe_ok:
            raise RuntimeError(
                "Fresh setup facts could not be collected, so driver-stage mutations are "
                "blocked. Reconnect and refresh facts before changing drivers."
            )
        if not self._needs_nvidia_recovery_group(self.ctx.facts or SetupFacts()):
            if self._skip_stage_if_current_audit_passes(SetupStage.POSTBOOT_DRIVER):
                return
        self._inject_nvidia_recovery_group_if_needed()
        self._grant_known_postboot_driver_capabilities()
        self._run_stage_groups(SetupStage.POSTBOOT_DRIVER)

    def _grant_known_postboot_driver_capabilities(self) -> None:
        store = self.ctx.capability_store
        if store is None:
            return
        for group in self._iter_stage_groups(SetupStage.POSTBOOT_DRIVER):
            for command in group.commands:
                self._grant_known_postboot_driver_command_capability(
                    SetupStage.POSTBOOT_DRIVER, group, command
                )

    def _grant_known_postboot_driver_command_capability(
        self,
        stage: SetupStage,
        group: CommandGroup,
        command: str,
    ) -> None:
        store = self.ctx.capability_store
        if store is None or stage != SetupStage.POSTBOOT_DRIVER:
            return
        if command not in _NVIDIA_HARNESS_CAPABILITY_COMMANDS:
            return
        commands = [*group.commands, *group.verification, command]
        if not self._is_nvidia_driver_group(stage, group, commands):
            return
        for requirement in dangerous_command_requirements(command):
            if requirement.get("kind") not in {"bootloader_config", "initramfs_rebuild"}:
                continue
            target = requirement.get("target", "")
            kind = requirement["kind"]
            if store.allows(kind=kind, target=target):
                continue
            preview_id = f"preview-nvidia-driver-{self.ctx.run_id}-{uuid.uuid4().hex[:8]}"
            grant = store.grant(
                kind=kind,
                target=target,
                confirmation_text="QuickHatch NVIDIA recovery boot artifact update",
                confirmation_id=preview_id,
                reason=requirement.get(
                    "reason",
                    "QuickHatch NVIDIA recovery boot artifact update",
                ),
                source="harness_generated",
                one_time_use=False,
                preview_id=preview_id,
            )
            self.runlog.record(
                "capability_granted",
                stage=stage.value,
                group_id=group.id,
                capability=asdict(grant),
            )

    def _run_desktop(self) -> None:
        if self._skip_stage_if_current_audit_passes(SetupStage.DESKTOP):
            return
        self._run_stage_groups(SetupStage.DESKTOP, audit_after=False)
        self._ensure_display_manager_started()
        stage_state = self.ctx.stage_state.setdefault(SetupStage.DESKTOP.value, {})
        self._update_desktop_display_audit_state(stage_state)
        result = audit_stage(
            SetupStage.DESKTOP,
            self.ctx.facts or SetupFacts(),
            plan=self.ctx.plan,
            stage_state=stage_state,
        )
        if not result.passed and self._resolve_display_preference_blockers(stage_state):
            result = audit_stage(
                SetupStage.DESKTOP,
                self.ctx.facts or SetupFacts(),
                plan=self.ctx.plan,
                stage_state=stage_state,
            )
        self._record_stage_result(result)

    def _skip_stage_if_current_audit_passes(self, stage: SetupStage) -> bool:
        if self.ctx.facts is None or self.ctx.plan is None:
            return False
        self._refresh_facts(stage=stage)
        stage_state = self.ctx.stage_state.setdefault(stage.value, {})
        if stage == SetupStage.DESKTOP:
            self._update_desktop_display_audit_state(stage_state)
        result = audit_stage(
            stage,
            self.ctx.facts or SetupFacts(),
            plan=self.ctx.plan,
            stage_state=stage_state,
        )
        if not result.passed:
            return False
        if stage == SetupStage.DESKTOP and not self._desktop_current_state_matches_plan():
            return False
        stage_state["skipped_current_state_already_valid"] = True
        self._record_stage_result(result)
        self.bridge.add_message(
            "agent",
            f"[harness] {stage.value} already satisfies audit after fact refresh; "
            "skipping planned mutation commands.",
        )
        self.runlog.record(
            "stage_skipped_current_audit_passed",
            stage=stage.value,
            result=asdict(result),
        )
        return True

    def _desktop_current_state_matches_plan(self) -> bool:
        if self.ctx.plan is None or self.ctx.facts is None:
            return True
        desired_text = " ".join(
            [
                self.ctx.plan.desktop_strategy.strategy,
                self.ctx.plan.desktop_strategy.notes,
                *(
                    " ".join([group.title, *group.commands, *group.verification])
                    for group in self._iter_stage_groups(SetupStage.DESKTOP)
                ),
            ]
        ).lower()
        desktop_env = (self.ctx.facts.desktop_design.environment or "").lower()
        display_manager = (self.ctx.facts.display_manager or "").lower()
        if any(token in desired_text for token in ("plasma", "kde", "sddm")):
            return "kde" in desktop_env or "plasma" in desktop_env or display_manager == "sddm"
        if any(token in desired_text for token in ("gnome", "gdm")):
            return "gnome" in desktop_env or display_manager == "gdm"
        if any(token in desired_text for token in ("xfce", "lightdm")):
            return "xfce" in desktop_env or display_manager == "lightdm"
        return True

    @staticmethod
    def _family_adapter_key(facts: SetupFacts) -> str:
        family = (facts.distro_family or "").strip().lower()
        if family in {"arch", "ubuntu", "debian", "fedora"}:
            return family
        os_id = (facts.os_id or "").strip().lower()
        if os_id in {"arch", "ubuntu", "debian", "fedora"}:
            return os_id
        package_manager = (facts.package_manager or "").strip().lower()
        if package_manager == "pacman":
            return "arch"
        if package_manager == "dnf":
            return "fedora"
        if package_manager == "apt":
            if os_id == "ubuntu" or "ubuntu" in (facts.os_like or "").lower():
                return "ubuntu"
            return "debian"
        return family or os_id or "unknown"

    def _enforce_live_family_install_spine(
        self, commands: list[str], verification: list[str]
    ) -> None:
        if self.ctx.plan is None or self.ctx.facts is None or not self.ctx.facts.is_live:
            return
        install_groups = self._iter_stage_groups(SetupStage.INSTALL_OR_SKIP)
        planned_commands = [command for group in install_groups for command in group.commands]
        planned_operations = [
            operation for group in install_groups for operation in group.operations
        ]
        disks = self._unique_disks(
            [
                *self._extract_destructive_target_disks(planned_commands),
                *self._extract_operation_target_disks(planned_operations),
            ]
        )
        install_state = self.ctx.stage_state.setdefault(SetupStage.INSTALL_OR_SKIP.value, {})
        if len(disks) != 1:
            install_state["family_adapter_spine_enforced"] = False
            install_state["family_adapter_spine_reason"] = (
                "No single explicit target disk was found in the researched install plan."
            )
            self.runlog.record(
                "family_adapter_spine_not_enforced",
                stage=SetupStage.INSTALL_OR_SKIP.value,
                reason=install_state["family_adapter_spine_reason"],
                detected_disks=disks,
            )
            raise RuntimeError(
                "Live install cannot continue without exactly one explicit confirmed target "
                "disk in the research plan. Stop and ask the user to choose the disk from "
                "`lsblk`; do not run model-invented partitioning commands."
            )
        disk_assignment = f"QUICKHATCH_TARGET_DISK={shlex.quote(disks[0])}; "
        spine_group = CommandGroup(
            id="quickhatch-family-install-spine",
            stage=SetupStage.INSTALL_OR_SKIP,
            title="Deterministic family live-install spine",
            commands=[disk_assignment + command for command in commands],
            verification=[disk_assignment + item for item in verification],
            rationale=(
                "QuickHatch owns disk/base/boot/reconnect mechanics; the model plan supplies "
                "the confirmed target disk and later stages."
            ),
            source="family_adapter",
        )
        self.ctx.plan.command_groups = [
            group
            for group in self.ctx.plan.command_groups
            if group.stage != SetupStage.INSTALL_OR_SKIP
        ]
        self.ctx.plan.command_groups.insert(0, spine_group)
        install_state["family_adapter_spine_enforced"] = True
        install_state["family_adapter_target_disk"] = disks[0]
        self.runlog.record(
            "family_adapter_spine_enforced",
            stage=SetupStage.INSTALL_OR_SKIP.value,
            target_disk=disks[0],
            replaced_group_count=len(install_groups),
            command_count=len(commands),
            verification_count=len(verification),
        )
        self._grant_family_spine_capabilities(disks[0], spine_group.commands)

    def _grant_family_spine_capabilities(self, target_disk: str, commands: list[str]) -> None:
        store = self.ctx.capability_store
        if store is None:
            return
        preview_id = f"preview-family-spine-{self.ctx.run_id}"
        requirements: dict[tuple[str, str], str] = {}
        disk_inventory = self._refresh_disk_inventory(
            stage=SetupStage.INSTALL_OR_SKIP,
            group_id="quickhatch-family-install-spine",
        )
        canonical_disk = canonicalize_target(target_disk, inventory=disk_inventory)
        esp_partition, root_partition = self._family_spine_partition_targets(canonical_disk)
        for command in commands:
            for requirement in dangerous_command_requirements(command):
                target = requirement.get("target") or ""
                kind = requirement["kind"]
                if kind == "disk_partition" and not target:
                    target = canonical_disk
                if kind == "format_filesystem" and not target:
                    for partition in (esp_partition, root_partition):
                        requirements[(kind, partition)] = requirement.get("reason", "")
                    continue
                if (
                    kind == "mount_target"
                    and target
                    and "->" not in target
                    and "unmount" not in (requirement.get("reason", "").lower())
                ):
                    canonical_mount = canonicalize_target(target, inventory=disk_inventory)
                    source = root_partition if canonical_mount == "/target-root" else esp_partition
                    target = f"{source} -> {target}"
                requirements[(kind, target)] = requirement.get("reason", "")
        if not requirements:
            return
        confirmation_token = f"ERASE {canonical_disk}"
        preview = DangerousOperationPreview(
            preview_id=preview_id,
            operation={"kind": "family_install_spine", "target": canonical_disk},
            rendered_commands=commands,
            install_profile=getattr(self.profile, "install_profile", "auto") or "auto",
            confirmation_token=confirmation_token,
            one_time_use=False,
            erases_data=True,
            disk_inventory=disk_inventory,
        )
        self.runlog.record(
            "dangerous_operation_preview",
            stage=SetupStage.INSTALL_OR_SKIP.value,
            group_id="quickhatch-family-install-spine",
            preview=asdict(preview),
        )
        self.bridge.add_message("agent", self._format_operation_preview(preview))
        answer = self._ask_blocking_question(
            (
                "QuickHatch is about to run the deterministic live-install spine. "
                "This will erase the selected target disk.\n\n"
                f"Required token: `{confirmation_token}`"
            ),
            choices=[confirmation_token, "stop setup"],
            stage=SetupStage.INSTALL_OR_SKIP,
            context={
                "group_id": "quickhatch-family-install-spine",
                "target_disk": canonical_disk,
                "preview_id": preview_id,
            },
            timeout=1800,
        )
        if (answer or "").strip() != confirmation_token:
            self._safe_abort(
                stage=SetupStage.INSTALL_OR_SKIP,
                group=None,
                reason=(
                    "Deterministic live-install spine was not confirmed with the "
                    f"required token `{confirmation_token}`."
                ),
            )
            raise RuntimeError(
                "Live install stopped because disk erase confirmation was not provided."
            )
        for (kind, target), reason in sorted(requirements.items()):
            grant = store.grant(
                kind=kind,
                target=target,
                confirmation_text=answer,
                confirmation_id=preview_id,
                reason=reason or "deterministic family install spine",
                source="user_confirmed",
                one_time_use=False,
                ttl_seconds=1800,
                disk_inventory=disk_inventory,
                preview_id=preview_id,
            )
            self.runlog.record(
                "capability_granted",
                stage=SetupStage.INSTALL_OR_SKIP.value,
                group_id="quickhatch-family-install-spine",
                capability=asdict(grant),
            )

    @staticmethod
    def _family_spine_partition_targets(target_disk: str) -> tuple[str, str]:
        suffix = "p" if str(target_disk or "").endswith(tuple("0123456789")) else ""
        return f"{target_disk}{suffix}1", f"{target_disk}{suffix}2"

    def _ensure_display_manager_started(self) -> None:
        facts = self.ctx.facts
        if (
            facts is None
            or facts.init_system != "systemd"
            or not facts.display_manager_enabled
            or facts.display_manager_active
        ):
            return
        command = (
            'if [ "$(id -u)" = "0" ]; then '
            "systemctl start display-manager.service; "
            "else sudo systemctl start display-manager.service; fi"
        )
        self.bridge.add_message(
            "agent",
            "Starting the enabled display manager so the target reaches a graphical login…",
        )
        rc, stdout, stderr = self._ssh_exec_with_transient_retries(
            command,
            60,
            stage=SetupStage.DESKTOP,
            group_id="quickhatch-start-display-manager",
            label="display-manager-start",
        )
        self.runlog.record(
            "display_manager_start",
            stage=SetupStage.DESKTOP.value,
            command=command,
            rc=rc,
            stdout_tail=(stdout or "")[-500:],
            stderr_tail=(stderr or "")[-500:],
        )
        self._wait_for_display_manager_state()

    def _wait_for_display_manager_state(self, *, attempts: int = 8) -> None:
        for attempt in range(max(1, attempts)):
            facts = self._refresh_facts(stage=SetupStage.DESKTOP)
            if facts.display_manager_active or not facts.display_manager_enabled:
                return
            if attempt < attempts - 1:
                time.sleep(min(2 + attempt, 5))

    def _update_desktop_display_audit_state(self, stage_state: dict[str, Any]) -> None:
        prefs = self._source_display_preferences()
        stage_state["display_preferences"] = prefs
        facts = self.ctx.facts
        if (
            prefs
            and facts is not None
            and not facts.displays
            and facts.desktop_runtime_state == DesktopRuntimeState.PENDING_RELOGIN
        ):
            target = next((pref for pref in prefs if pref.get("primary")), prefs[0])
            mode = str(target.get("mode") or "").strip()
            refresh = self._as_float(target.get("refresh_hz"))
            label = mode + (f"@{refresh:g}Hz" if refresh is not None and mode else "")
            stage_state["display_manual_followup"] = True
            stage_state["display_manual_followup_reason"] = (
                "Display preferences"
                + (f" ({label})" if label else "")
                + " could not be runtime-verified before graphical login; display manager "
                "is enabled, so treat display tuning as post-login/manual follow-up."
            )
        blockers = self._display_preference_blockers(prefs)
        stage_state["display_preference_blockers"] = blockers
        if blockers:
            reason = "Display tuning left for manual follow-up: " + " | ".join(blockers)
            stage_state["display_manual_followup"] = True
            stage_state["display_manual_followup_reason"] = reason
            manual_followups = list(stage_state.get("manual_followups") or [])
            item = {"reason": reason}
            if item not in manual_followups:
                manual_followups.append(item)
            stage_state["manual_followups"] = manual_followups
        elif facts is not None and facts.displays:
            self._clear_stale_display_manual_followup(stage_state)

    @staticmethod
    def _clear_stale_display_manual_followup(stage_state: dict[str, Any]) -> None:
        def is_stale_display_reason(value: Any) -> bool:
            reason = str(value or "").strip().lower()
            if "display" not in reason:
                return False
            return any(
                marker in reason
                for marker in (
                    "no active target display",
                    "runtime-verified",
                    "display tuning left for manual follow-up",
                )
            )

        reason = stage_state.get("display_manual_followup_reason")
        if is_stale_display_reason(reason):
            stage_state.pop("display_manual_followup", None)
            stage_state.pop("display_manual_followup_reason", None)
            stage_state.pop("skipped_runtime_desktop_items", None)

        manual_followups = list(stage_state.get("manual_followups") or [])
        if manual_followups:
            filtered = [
                item
                for item in manual_followups
                if not is_stale_display_reason(
                    item.get("reason") if isinstance(item, dict) else item
                )
            ]
            if filtered:
                stage_state["manual_followups"] = filtered
            else:
                stage_state.pop("manual_followups", None)

    def _resolve_display_preference_blockers(self, stage_state: dict[str, Any]) -> bool:
        blockers = [str(item) for item in (stage_state.get("display_preference_blockers") or [])]
        if not blockers:
            return False
        answer = self._ask_blocking_question(
            (
                "QuickHatch detected a target display mismatch after desktop setup.\n\n"
                + "\n".join(f"- {blocker}" for blocker in blockers)
                + "\n\nChoose retry if the monitor was asleep, on another input, or the "
                "graphical session just started. Choose manual follow-up if this monitor "
                "cannot expose the requested mode right now or you want to finish setup "
                "and adjust display settings later."
            ),
            choices=["retry display probe", "manual follow-up"],
            stage=SetupStage.DESKTOP,
            context={"display_preference_blockers": blockers},
            timeout=600,
            return_qid=False,
        )
        if answer and answer.strip().lower().startswith("retry"):
            self._refresh_facts(stage=SetupStage.DESKTOP)
            self._update_desktop_display_audit_state(stage_state)
            blockers = [
                str(item) for item in (stage_state.get("display_preference_blockers") or [])
            ]
            if not blockers:
                return True

        reason = "Display tuning left for manual follow-up: " + " | ".join(blockers)
        stage_state["display_manual_followup"] = True
        stage_state["display_manual_followup_reason"] = reason
        stage_state["display_preference_blockers"] = []
        stage_state.setdefault("manual_followups", []).append({"reason": reason})
        self.runlog.record(
            "display_preference_manual_followup",
            stage=SetupStage.DESKTOP.value,
            reason=reason,
        )
        self.bridge.add_message("agent", f"⚠️ {reason}")
        return True

    def _desktop_runtime_ready_or_followup(
        self, group: CommandGroup, stage_state: dict[str, Any]
    ) -> bool:
        if not self._desktop_group_is_runtime_display_only(group):
            return True
        if self.ctx.facts and self.ctx.facts.displays:
            return True
        self._refresh_facts(stage=SetupStage.DESKTOP)
        if self.ctx.facts and self.ctx.facts.displays:
            return True

        answer = self._ask_blocking_question(
            (
                "QuickHatch cannot read an active target display before running "
                f"desktop live-apply group '{group.title}'. If the monitor is asleep, "
                "on another input, or not logged into Plasma/SDDM, switch it to this "
                "Linux machine and choose retry. Otherwise leave wallpaper/resolution/"
                "live theme apply as manual follow-up."
            ),
            choices=["retry display probe", "manual follow-up"],
            stage=SetupStage.DESKTOP,
            context={"group_id": group.id, "display_runtime_probe": True},
            timeout=600,
            return_qid=False,
        )
        if answer and answer.strip().lower().startswith("retry"):
            self._refresh_facts(stage=SetupStage.DESKTOP)
            if self.ctx.facts and self.ctx.facts.displays:
                return True

        reason = (
            f"Skipped display-dependent desktop group '{group.title}' because no "
            "active target display facts were available over the real GUI session."
        )
        stage_state["display_manual_followup"] = True
        stage_state["display_manual_followup_reason"] = reason
        self.bridge.add_message("agent", f"⚠️ {reason}")
        return False

    @staticmethod
    def _desktop_group_is_runtime_display_only(group: CommandGroup) -> bool:
        items = [("command", command) for command in group.commands if str(command).strip()] + [
            ("verification", command) for command in group.verification if str(command).strip()
        ]
        return bool(items) and all(
            SetupOrchestrator._command_needs_desktop_runtime_env(command)
            and not (
                kind == "command"
                and SetupOrchestrator._desktop_command_can_persist_without_gui(command)
            )
            for kind, command in items
        )

    def _desktop_runtime_command_ready_or_followup(
        self,
        command: str,
        group: CommandGroup,
        stage_state: dict[str, Any],
        *,
        item_kind: str,
    ) -> bool:
        if not self._command_needs_desktop_runtime_env(command):
            return True
        if item_kind == "command" and self._desktop_command_can_persist_without_gui(command):
            return True
        if self.ctx.facts and self.ctx.facts.displays:
            return True
        self._refresh_facts(stage=SetupStage.DESKTOP)
        if self.ctx.facts and self.ctx.facts.displays:
            return True

        asked_groups = set(stage_state.get("_display_runtime_groups_asked") or [])
        if group.id not in asked_groups:
            answer = self._ask_blocking_question(
                (
                    "QuickHatch cannot read an active target display before running "
                    f"desktop live-apply {item_kind} in group '{group.title}'. If the "
                    "monitor is asleep, on another input, or not logged into the GUI, "
                    "switch it to this Linux machine and choose retry. Otherwise this "
                    "live-only item will be left as manual follow-up while non-display "
                    "install/config commands continue."
                ),
                choices=["retry display probe", "manual follow-up"],
                stage=SetupStage.DESKTOP,
                context={
                    "group_id": group.id,
                    "display_runtime_probe": True,
                    "item_kind": item_kind,
                    "command": command,
                },
                timeout=600,
                return_qid=False,
            )
            asked_groups.add(group.id)
            stage_state["_display_runtime_groups_asked"] = sorted(asked_groups)
            if answer and answer.strip().lower().startswith("retry"):
                self._refresh_facts(stage=SetupStage.DESKTOP)
                if self.ctx.facts and self.ctx.facts.displays:
                    return True

        reason = (
            f"Skipped display-dependent desktop {item_kind} in group '{group.title}' "
            "because no active target display facts were available over the real GUI "
            "session."
        )
        stage_state["display_manual_followup"] = True
        stage_state["display_manual_followup_reason"] = reason
        stage_state.setdefault("skipped_runtime_desktop_items", []).append(
            {"group_id": group.id, "kind": item_kind, "command": command}
        )
        self.runlog.record(
            "desktop_runtime_item_manual_followup",
            stage=SetupStage.DESKTOP.value,
            group_id=group.id,
            item_kind=item_kind,
            command=command,
            reason=reason,
        )
        self.bridge.add_message("agent", f"⚠️ {reason}")
        return False

    def _source_display_preferences(self) -> list[dict[str, Any]]:
        displays = getattr(getattr(self.profile, "hardware", None), "displays", []) or []
        prefs: list[dict[str, Any]] = []
        for display in displays:
            width = int(getattr(display, "width_px", 0) or 0)
            height = int(getattr(display, "height_px", 0) or 0)
            if width <= 0 or height <= 0:
                continue
            prefs.append(
                {
                    "name": getattr(display, "name", "") or "display",
                    "mode": f"{width}x{height}",
                    "refresh_hz": getattr(display, "refresh_hz", None),
                    "primary": bool(getattr(display, "primary", False)),
                }
            )
        return prefs

    def _display_preference_blockers(self, prefs: list[dict[str, Any]]) -> list[str]:
        if not prefs or self.ctx.facts is None:
            return []
        preferred = next((pref for pref in prefs if pref.get("primary")), prefs[0])
        preferred_mode = str(preferred.get("mode") or "")
        preferred_refresh = self._as_float(preferred.get("refresh_hz"))
        target_label = preferred_mode
        if preferred_refresh is not None:
            target_label += f"@{preferred_refresh:g}Hz"
        displays = list(self.ctx.facts.displays or [])
        if not displays:
            return [
                "Display preferences could not be runtime-verified"
                + (f" ({target_label})" if target_label else "")
                + "; no active target display facts were available. Log into the graphical session, "
                "rerun desktop verification, or leave display tuning as manual follow-up."
            ]
        matching_mode = [
            display
            for display in displays
            if not preferred_mode or display.current_mode == preferred_mode
        ]
        if preferred_mode and not matching_mode:
            seen = ", ".join(
                f"{display.name}:{display.current_mode or 'unknown'}" for display in displays
            )
            return [
                f"Display mode mismatch: preferred {target_label}, target reports {seen}. "
                "Adjust KDE/kscreen display configuration before declaring desktop complete."
            ]
        if preferred_refresh is not None and matching_mode:
            known_refresh = [
                display.refresh_hz for display in matching_mode if display.refresh_hz is not None
            ]
            if not known_refresh:
                return [
                    f"Display refresh could not be verified for preferred {target_label}. "
                    "Verify the active monitor refresh rate before declaring desktop complete."
                ]
            if not any(
                abs(float(refresh) - float(preferred_refresh)) <= 1.0 for refresh in known_refresh
            ):
                seen = ", ".join(f"{refresh:g}Hz" for refresh in known_refresh)
                return [
                    f"Display refresh mismatch: preferred {target_label}, target reports {seen}. "
                    "Adjust KDE/kscreen refresh rate before declaring desktop complete."
                ]
        return []

    def _desktop_design_feedback_failure(
        self,
        group: CommandGroup,
        stage_state: dict[str, Any],
    ) -> GroupFailure | None:
        result = audit_stage(
            SetupStage.DESKTOP,
            self.ctx.facts or SetupFacts(),
            plan=self.ctx.plan,
            stage_state=stage_state,
        )
        design_followups = [
            str(item)
            for item in (result.details.get("desktop_design_manual_followups") or [])
            if str(item or "").strip()
        ]
        self._replace_desktop_design_feedback_followups(stage_state, design_followups)
        relevant_manual_followups = [
            str(item.get("reason") or "")
            for item in (stage_state.get("manual_followups") or [])
            if isinstance(item, dict)
            and item.get("source")
            in {
                "desktop_design_feedback",
                "desktop_wrong_environment",
                "desktop_wrong_user",
            }
            and str(item.get("reason") or "").strip()
        ]
        if not design_followups and not relevant_manual_followups:
            return None
        details = relevant_manual_followups or design_followups
        self.runlog.record(
            "desktop_design_feedback_mismatch",
            stage=SetupStage.DESKTOP.value,
            group_id=group.id,
            details=details,
            facts=asdict(self.ctx.facts or SetupFacts()),
        )
        facts = self.ctx.facts or SetupFacts()
        has_active_session = bool(facts.displays) and (
            facts.desktop_runtime_state == DesktopRuntimeState.LIVE_APPLY_SUCCEEDED
        )
        if not has_active_session:
            return None
        return GroupFailure(
            failure_type="desktop_design_feedback",
            command="desktop design verification",
            stdout="",
            stderr="",
            detail="; ".join(details),
        )

    @staticmethod
    def _as_float(value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _run_apps(self) -> None:
        self._mark_installed_apps_from_native_package_db()
        self._resolve_requested_apps_from_catalog()
        self._synthesize_missing_app_command_groups()
        has_explicit_app_groups_without_app_rows = bool(
            self.ctx.plan
            and not self.ctx.plan.apps
            and any(group.stage == SetupStage.APPS for group in self.ctx.plan.command_groups)
        )
        if (
            not has_explicit_app_groups_without_app_rows
            and self._skip_stage_if_current_audit_passes(SetupStage.APPS)
        ):
            return
        self._run_stage_groups(SetupStage.APPS)

    def _run_final_audit(self) -> AuditResult:
        self.bridge.update_setup_run(stage=SetupStage.FINAL_AUDIT.value, status="running")
        final_state = self.ctx.stage_state.setdefault(SetupStage.FINAL_AUDIT.value, {})
        repairs: list[dict[str, Any]] = []
        result = AuditResult(stage=SetupStage.FINAL_AUDIT, passed=False)
        for repair_attempt in range(self.FINAL_AUDIT_REPAIRS + 1):
            result = self._compute_final_audit_result()
            repairable = self._repairable_final_findings(result)
            if not repairable or repair_attempt >= self.FINAL_AUDIT_REPAIRS:
                break
            repaired = self._repair_final_audit_findings(
                repairable,
                repair_attempt=repair_attempt + 1,
                final_state=final_state,
            )
            repairs.append(repaired)
            if not repaired.get("executed"):
                break
            self._refresh_facts(stage=SetupStage.FINAL_AUDIT)
        if repairs:
            result.details["final_audit_repairs"] = repairs
            failed_repairs = [
                repair
                for repair in repairs
                if not repair.get("executed") and repair.get("action") != "manual_followup"
            ]
            warning_only_after_failed_repair = self._failed_final_audit_repairs_are_warning_only(
                result, failed_repairs
            )
            if (
                failed_repairs
                and result.final_status != FinalSetupStatus.BLOCKED
                and not warning_only_after_failed_repair
            ):
                result.findings.append(
                    AuditFinding(
                        id="final_audit:repair_failed",
                        severity=AuditFindingSeverity.MANUAL_FOLLOWUP,
                        category="setup",
                        summary=(
                            "Final audit repair could not be applied safely; "
                            "manual follow-up is required."
                        ),
                        evidence=[
                            str(item.get("error") or item.get("action") or "repair failed")
                            for item in failed_repairs
                        ],
                        suggested_repair_scope="review final audit repair failure",
                    )
                )
                result.final_status = FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED
            if result.final_status != FinalSetupStatus.BLOCKED and any(
                repair.get("action") == "manual_followup" for repair in repairs
            ):
                result.final_status = FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED
        if result.final_status is None:
            result.final_status = self._final_status_from_findings(result)
        self.ctx.stage_results.append(result)
        self.runlog.record("final_audit", result=asdict(result))
        if result.final_status == FinalSetupStatus.BLOCKED:
            status = "blocked"
        elif result.final_status == FinalSetupStatus.SUCCESS:
            status = "done"
        elif result.final_status in {
            FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED,
            FinalSetupStatus.UNSUPPORTED_SAFE,
            FinalSetupStatus.SUCCESS_WITH_WARNINGS,
        }:
            status = result.final_status.value
        else:
            status = "done"
        self.bridge.update_setup_run(
            blockers=list(result.blockers),
            status=status,
            stage=SetupStage.FINAL_AUDIT.value,
            final_status=result.final_status.value if result.final_status else "",
        )
        return result

    def _compute_final_audit_result(self) -> AuditResult:
        if self.ctx.facts is not None:
            self._refresh_facts(stage=SetupStage.FINAL_AUDIT)
        if self.ctx.plan is None:
            result = AuditResult(
                stage=SetupStage.FINAL_AUDIT,
                passed=False,
                blockers=["No research plan exists for final audit."],
            )
        else:
            latest_results = self._latest_stage_results(exclude_final=True)
            result = final_audit(self.ctx.facts or SetupFacts(), self.ctx.plan, latest_results)
            fresh_audits: dict[str, dict[str, Any]] = {}
            for fresh_stage in (
                SetupStage.POSTBOOT_DRIVER,
                SetupStage.DESKTOP,
                SetupStage.APPS,
            ):
                fresh_result = audit_stage(
                    fresh_stage,
                    self.ctx.facts or SetupFacts(),
                    plan=self.ctx.plan,
                    stage_state=self.ctx.stage_state.get(fresh_stage.value, {}),
                )
                latest_results = [item for item in latest_results if item.stage != fresh_stage] + [
                    fresh_result
                ]
                fresh_audits[fresh_stage.value] = asdict(fresh_result)
            result = final_audit(self.ctx.facts or SetupFacts(), self.ctx.plan, latest_results)
            result.details["fresh_stage_audits"] = fresh_audits
            remote_blockers = self._remote_final_verification()
            result.details["remote_final_verification"] = dict(
                self.ctx.stage_state.get(SetupStage.FINAL_AUDIT.value, {})
            )
            if remote_blockers:
                result.findings.extend(
                    findings_from_blockers(SetupStage.FINAL_AUDIT, remote_blockers)
                )
                result.blockers = [
                    finding.summary
                    for finding in result.findings
                    if finding.severity.value == "fatal"
                ]
                result.passed = not result.blockers
                result.final_status = self._final_status_from_findings(result)
                result.details["remote_final_verification_blockers"] = remote_blockers
        if result.final_status is None:
            result.final_status = self._final_status_from_findings(result)
        return result

    @staticmethod
    def _repairable_final_findings(result: AuditResult) -> list[AuditFinding]:
        return [
            finding
            for finding in result.findings
            if finding.severity == AuditFindingSeverity.REPAIRABLE
        ]

    def _repair_final_audit_findings(
        self,
        findings: list[AuditFinding],
        *,
        repair_attempt: int,
        final_state: dict[str, Any],
    ) -> dict[str, Any]:
        summaries = [finding.summary for finding in findings]
        repair_stage = self._final_audit_repair_stage(findings)
        group = CommandGroup(
            id=f"final-audit-repair-{repair_attempt}",
            stage=repair_stage,
            title="Focused final audit repair",
            commands=[],
            verification=[],
            evidence=[{"finding": summary} for summary in summaries],
        )
        failure = GroupFailure(
            failure_type="final_audit",
            command="final audit",
            stdout="",
            stderr="",
            detail="; ".join(summaries[:5]),
        )
        self.bridge.add_message(
            "agent",
            (
                "Final audit found repairable issue(s); requesting a focused repair "
                f"plan (attempt {repair_attempt}/{self.FINAL_AUDIT_REPAIRS})…"
            ),
        )
        try:
            outcome = self._repair_failed_group(
                repair_stage,
                group,
                failure,
                repair_attempt=repair_attempt,
            )
        except RuntimeError as exc:
            record = {"executed": False, "error": str(exc), "findings": summaries}
            final_state.setdefault("final_audit_repair_failures", []).append(record)
            self.runlog.record("final_audit_repair_failed", **record)
            return record
        action = str(outcome.get("action", ""))
        record: dict[str, Any] = {
            "executed": False,
            "action": action,
            "findings": summaries,
        }
        if action == "manual_followup":
            record["reason"] = str(outcome.get("reason", "") or "manual follow-up")
            final_state.setdefault("manual_followups", []).append(
                {"reason": record["reason"], "findings": summaries}
            )
            self.runlog.record("final_audit_repair_manual_followup", **record)
            return record
        if action != "retry_group":
            record["error"] = f"Unsupported final-audit repair action: {action}"
            self.runlog.record("final_audit_repair_failed", **record)
            return record
        repair_group = outcome["group"]
        repair_failure = self._execute_group(repair_stage, repair_group, final_state)
        record["executed"] = repair_failure is None
        record["stage"] = repair_stage.value
        record["group"] = asdict(repair_group)
        if repair_failure is not None:
            record["error"] = repair_failure.detail
        final_state.setdefault("final_audit_repairs", []).append(record)
        self.runlog.record("final_audit_repair_executed", **record)
        return record

    @staticmethod
    def _final_audit_repair_stage(findings: list[AuditFinding]) -> SetupStage:
        categories = {finding.category for finding in findings}
        if categories & {"fstab", "bootability", "ssh_reconnect"}:
            return SetupStage.INSTALL_OR_SKIP
        if "driver" in categories:
            return SetupStage.POSTBOOT_DRIVER
        if "desktop" in categories:
            return SetupStage.DESKTOP
        return SetupStage.APPS

    @staticmethod
    def _failed_final_audit_repairs_are_warning_only(
        result: AuditResult,
        failed_repairs: list[dict[str, Any]],
    ) -> bool:
        if result.final_status != FinalSetupStatus.SUCCESS_WITH_WARNINGS or not result.findings:
            return False
        if all(finding.severity == AuditFindingSeverity.WARNING for finding in result.findings):
            return True
        if any(
            finding.severity not in {AuditFindingSeverity.WARNING, AuditFindingSeverity.REPAIRABLE}
            for finding in result.findings
        ):
            return False
        text = " ".join(
            [
                *(finding.summary for finding in result.findings),
                *(
                    str(repair.get("error") or repair.get("action") or "")
                    for repair in failed_repairs
                ),
            ]
        ).lower()
        return any(marker in text for marker in ("optional", "non-critical", "noncritical"))

    @staticmethod
    def _final_status_from_findings(result: AuditResult) -> FinalSetupStatus:
        severities = {finding.severity for finding in result.findings}
        severity_values = {
            str(severity.value if hasattr(severity, "value") else severity)
            for severity in severities
        }
        if "fatal" in severity_values:
            return FinalSetupStatus.BLOCKED
        if "manual_followup" in severity_values:
            return FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED
        if "unsupported_safe" in severity_values:
            return FinalSetupStatus.UNSUPPORTED_SAFE
        if result.findings:
            return FinalSetupStatus.SUCCESS_WITH_WARNINGS
        return FinalSetupStatus.SUCCESS

    def _latest_stage_results(self, *, exclude_final: bool = False) -> list[AuditResult]:
        latest: dict[SetupStage, AuditResult] = {}
        for result in self.ctx.stage_results:
            if exclude_final and result.stage == SetupStage.FINAL_AUDIT:
                continue
            latest[result.stage] = result
        return list(latest.values())

    def _remote_final_verification(self) -> list[str]:
        if self.ctx.plan is None or self.ctx.facts is None:
            return []
        blockers: list[str] = []
        blockers.extend(self._verify_not_live_environment())
        blockers.extend(self._verify_planned_packages_installed())
        blockers.extend(self._verify_fstab_root_entry())
        blockers.extend(self._verify_boot_artifacts())
        blockers.extend(self._verify_reboot_required_state())
        blockers.extend(self._verify_systemd_health())
        blockers.extend(self._verify_display_manager_session_health())
        return blockers

    def _verify_not_live_environment(self) -> list[str]:
        if self.ctx.facts is None or not self.ctx.facts.is_live:
            return []
        return [
            "Target is still reporting a live environment during final audit; "
            "the installed system has not been proven booted/reconnectable."
        ]

    def _verify_reboot_required_state(self) -> list[str]:
        if self.ctx.facts is None or self.ctx.facts.is_live:
            return []
        final_state = self.ctx.stage_state.setdefault(SetupStage.FINAL_AUDIT.value, {})
        command = r"""qh_required=no
qh_detail=
if [ -f /var/run/reboot-required ] || [ -f /run/reboot-required ]; then
  qh_required=yes
  qh_detail="reboot-required marker present"
  if [ -f /var/run/reboot-required.pkgs ]; then
    qh_detail="$qh_detail: $(tr '\n' ' ' < /var/run/reboot-required.pkgs 2>/dev/null)"
  fi
elif command -v needs-restarting >/dev/null 2>&1; then
  qh_out=$(needs-restarting -r 2>&1)
  qh_rc=$?
  if [ "$qh_rc" -ne 0 ] && printf '%s' "$qh_out" | grep -Eiq 'reboot|restart.*system|core libraries|kernel'; then
    qh_required=yes
    qh_detail="$qh_out"
  fi
elif command -v dkms >/dev/null 2>&1; then
  qh_out=$(dkms status 2>/dev/null | grep -Ei ': (added|built)$|: (added|built),' || true)
  if [ -n "$qh_out" ]; then
    qh_required=yes
    qh_detail="DKMS module build/install is incomplete: $qh_out"
  fi
fi
printf 'QH_REBOOT_REQUIRED=%s\n' "$qh_required"
printf 'QH_REBOOT_DETAIL=%s\n' "$qh_detail"
"""
        rc, stdout, stderr = self._ssh_exec_with_transient_retries(
            command,
            45,
            stage=SetupStage.FINAL_AUDIT,
            group_id="final-reboot-required",
            label="final reboot-required verification",
        )
        self.runlog.record(
            "final_reboot_required_verification",
            command=command,
            rc=rc,
            stdout_tail=(stdout or "")[-800:],
            stderr_tail=(stderr or "")[-800:],
        )
        final_state["reboot_required_verification_ran"] = True
        final_state["reboot_required_rc"] = rc
        final_state["reboot_required_raw"] = (stdout or "")[-1000:]
        if rc != 0:
            return [
                "Could not verify reboot-required state: "
                + (stderr or stdout or "unknown error").strip()[:240]
            ]
        required = ""
        detail = ""
        for line in (stdout or "").splitlines():
            if line.startswith("QH_REBOOT_REQUIRED="):
                required = line.split("=", 1)[1].strip()
            elif line.startswith("QH_REBOOT_DETAIL="):
                detail = line.split("=", 1)[1].strip()
        final_state["reboot_required"] = required
        final_state["reboot_required_detail"] = detail
        if required == "yes":
            return [
                "A reboot is still required before setup can be declared complete"
                + (f": {detail[:240]}" if detail else ".")
            ]
        return []

    def _verify_planned_packages_installed(self) -> list[str]:
        if self.ctx.plan is None or self.ctx.facts is None:
            return []
        final_state = self.ctx.stage_state.setdefault(SetupStage.FINAL_AUDIT.value, {})
        final_state["package_verification_ran"] = False
        final_state["verified_installed_apps"] = []
        final_state["verified_alternative_apps"] = []
        final_state["unverified_installed_apps"] = []
        installed_like = [
            app
            for app in self.ctx.plan.apps
            if app.status in {PlanItemStatus.INSTALLED, PlanItemStatus.COMPLETED}
        ]
        aliases_by_app = {app.name: self._app_verification_aliases(app) for app in installed_like}
        candidates = [app for app in installed_like if self._app_has_verification_aliases(app)]
        unverifiable = [app.name for app in installed_like if app not in candidates]
        final_state["unverified_installed_apps"] = sorted(unverifiable)
        if unverifiable:
            self._record_app_verification_manual_followup(
                final_state,
                unverifiable,
                "missing package, Flatpak, Snap, binary, or desktop-entry identifiers",
            )
            required_unverifiable = [
                app.name for app in installed_like if app not in candidates and app.required
            ]
            if required_unverifiable:
                return [
                    "Manual follow-up app(s) were claimed installed/completed but cannot be "
                    "verified without package, Flatpak, Snap, binary, or desktop-entry "
                    "identifiers: " + ", ".join(sorted(required_unverifiable))
                ]
        if not candidates:
            return []
        native_candidates = [app for app in candidates if aliases_by_app[app.name].native_packages]
        flatpak_candidates = [app for app in candidates if aliases_by_app[app.name].flatpak_ids]
        snap_candidates = [app for app in candidates if aliases_by_app[app.name].snap_names]
        binary_candidates = [app for app in candidates if aliases_by_app[app.name].binaries]
        desktop_candidates = [app for app in candidates if aliases_by_app[app.name].desktop_entries]
        query_cmds = {
            "pacman": "pacman -Qq",
            "apt": (
                "dpkg-query -W -f='${db:Status-Abbrev} ${Package}\\n' 2>/dev/null "
                "| awk '$1 == \"ii\" {print $2}'"
            ),
            "dnf": "rpm -qa --queryformat '%{NAME}\\n'",
            "zypper": "rpm -qa --queryformat '%{NAME}\\n'",
            "apk": "apk info",
            "xbps": "xbps-query -m | sed 's/-[0-9][^-]*$//'",
            "emerge": "qlist -IC 2>/dev/null || true",
        }
        query = query_cmds.get(self.ctx.facts.package_manager)
        if not query and native_candidates:
            self._record_app_verification_manual_followup(
                final_state,
                [app.name for app in native_candidates],
                f"no package query command for {self.ctx.facts.package_manager}",
            )
            native_candidates = []
            if not any(
                (flatpak_candidates, snap_candidates, binary_candidates, desktop_candidates)
            ):
                return []
        installed: set[str] = set()
        # Query the native package DB even for Flatpak-planned apps when possible.
        # Repairs may choose a faster/better repo package while the original plan item
        # still names a Flatpak id, and final audit should verify the real outcome.
        if candidates and query:
            final_state["package_verification_ran"] = True
            rc, stdout, stderr = self._ssh_exec_with_transient_retries(
                query,
                45,
                stage=SetupStage.FINAL_AUDIT,
                group_id="final-package-verification",
                label="final package verification",
            )
            self.runlog.record(
                "final_package_verification",
                package_manager=self.ctx.facts.package_manager,
                command=query,
                rc=rc,
                stdout_tail=(stdout or "")[-800:],
                stderr_tail=(stderr or "")[-800:],
            )
            if rc != 0:
                final_state["package_verification_error"] = (
                    stderr or stdout or "package query failed"
                ).strip()[:500]
            else:
                installed = {
                    line.strip().lower() for line in (stdout or "").splitlines() if line.strip()
                }
        flatpak_installed = self._query_installed_flatpaks() if flatpak_candidates else set()
        snap_installed = self._query_installed_snaps() if snap_candidates else set()
        completed_group_tokens = self._completed_app_group_verification_tokens()
        missing_apps: list[AppPlanItem] = []
        verified: list[AppVerificationMatch] = []
        alternative_verified: list[AppVerificationMatch] = []
        for app in candidates:
            aliases = aliases_by_app[app.name]
            match = self._match_app_verification_signal(
                app,
                aliases,
                native_installed=installed,
                flatpak_installed=flatpak_installed,
                snap_installed=snap_installed,
                binary_installed=set(),
                desktop_entries=set(),
                completed_group_tokens=completed_group_tokens,
            )
            if match is None:
                missing_apps.append(app)
                continue
            if match.alternative:
                alternative_verified.append(match)
            else:
                verified.append(match)
        if missing_apps:
            binary_installed = (
                self._query_installed_binaries(
                    sorted(
                        {
                            alias
                            for app in missing_apps
                            for alias in aliases_by_app[app.name].binaries
                        }
                    )
                )
                if any(aliases_by_app[app.name].binaries for app in missing_apps)
                else set()
            )
            desktop_entries = (
                self._query_desktop_app_entries()
                if any(aliases_by_app[app.name].desktop_entries for app in missing_apps)
                else set()
            )
            still_missing: list[AppPlanItem] = []
            for app in missing_apps:
                aliases = aliases_by_app[app.name]
                match = self._match_app_verification_signal(
                    app,
                    aliases,
                    native_installed=installed,
                    flatpak_installed=flatpak_installed,
                    snap_installed=snap_installed,
                    binary_installed=binary_installed,
                    desktop_entries=desktop_entries,
                    completed_group_tokens=completed_group_tokens,
                )
                if match is None:
                    still_missing.append(app)
                    continue
                if match.alternative:
                    alternative_verified.append(match)
                else:
                    verified.append(match)
            missing_apps = still_missing
        missing = [app.name for app in missing_apps]
        final_state["verified_installed_apps"] = sorted(match.app_name for match in verified)
        final_state["verified_alternative_apps"] = [
            {
                "app": match.app_name,
                "alternative": match.alternative_name or match.target,
                "signal": match.signal,
                "target": match.target,
            }
            for match in sorted(alternative_verified, key=lambda item: item.app_name)
        ]
        final_state["app_verification_signals"] = {
            match.app_name: {
                "signal": match.signal,
                "target": match.target,
                "alternative": match.alternative,
                "alternative_name": match.alternative_name,
            }
            for match in [*verified, *alternative_verified]
        }
        final_state["unverified_installed_apps"] = sorted({*unverifiable, *missing})
        blockers: list[str] = []
        if alternative_verified:
            blockers.append(
                "Alternative installed for requested app(s): "
                + ", ".join(
                    sorted(
                        f"{match.app_name} -> {match.alternative_name or match.target}"
                        for match in alternative_verified
                    )
                )
            )
        if not missing:
            return blockers
        self._record_app_verification_manual_followup(
            final_state,
            missing,
            "installed app was not found through package, Flatpak, Snap, binary, desktop-entry, "
            "or completed app-group verification",
        )
        required_missing = [
            app.name for app in candidates if app.name in set(missing) and app.required
        ]
        if required_missing:
            blockers.append(
                "Manual follow-up required app(s) were claimed installed/completed but final "
                "verification did not find them through package, Flatpak, Snap, binary, "
                "desktop-entry, or completed app-group signals: "
                + ", ".join(sorted(required_missing))
            )
        optional_missing = sorted(set(missing) - set(required_missing))
        if optional_missing:
            blockers.append(
                "Manual follow-up optional app(s) were claimed installed/completed but final "
                "verification did not find them: " + ", ".join(optional_missing)
            )
        return blockers

    def _app_has_verification_aliases(self, app: AppPlanItem) -> bool:
        aliases = self._app_verification_aliases(app)
        return any(
            (
                aliases.native_packages,
                aliases.flatpak_ids,
                aliases.snap_names,
                aliases.binaries,
                aliases.desktop_entries,
            )
        )

    def _app_verification_aliases(self, app: AppPlanItem) -> AppVerificationAliases:
        aliases = AppVerificationAliases()
        source = self._normalized_app_source(app.source)
        explicit_alternative = self._app_is_explicit_alternative(app)

        def add_name(value: str) -> None:
            variants = self._package_name_variants(value, "")
            aliases.native_packages.update(variants)
            aliases.desktop_entries.update(variants)
            aliases.binaries.update(self._binary_aliases_from_variants(variants))

        def add_package(value: str, package_source: str = "") -> None:
            package = str(value or "").strip()
            if not package:
                return
            lowered = package.lower()
            variants = self._package_name_variants("", package)
            aliases.native_packages.update(variants)
            aliases.desktop_entries.update(variants)
            aliases.binaries.update(self._binary_aliases_from_variants(variants))
            normalized_source = self._normalized_app_source(package_source)
            if normalized_source in {"flatpak", "flathub"} or self._looks_like_flatpak_id(package):
                aliases.flatpak_ids.add(lowered)
            if normalized_source == "snap":
                aliases.snap_names.add(lowered)

        add_name(app.name)
        add_package(app.package_name, source)

        entry = match_catalog_entry(app.name)
        if entry is not None:
            for name in (entry.name, *entry.match_names):
                add_name(name)
            for family in self._catalog_family_candidates():
                hint = entry.install_hints.get(family) or {}
                hint_source = str(hint.get("source") or "")
                if (
                    self._normalized_app_source(hint_source) == "alternative"
                    and not explicit_alternative
                ):
                    continue
                for package in hint.get("packages", []) or []:
                    add_package(str(package), hint_source)

        if explicit_alternative:
            add_package(app.package_name, source)
            aliases.desktop_entries.update(self._package_name_variants(app.reason, ""))

        return AppVerificationAliases(
            native_packages=self._clean_aliases(aliases.native_packages),
            flatpak_ids=self._clean_aliases(aliases.flatpak_ids),
            snap_names=self._clean_aliases(aliases.snap_names),
            binaries=self._drop_weak_binary_desktop_aliases(
                self._clean_executable_aliases(aliases.binaries)
            ),
            desktop_entries=self._drop_weak_binary_desktop_aliases(
                self._clean_aliases(aliases.desktop_entries)
            ),
        )

    def _catalog_family_candidates(self) -> list[str]:
        facts = self.ctx.facts or SetupFacts()
        plan = self.ctx.plan
        raw = " ".join(
            item
            for item in (
                facts.distro_family,
                facts.os_id,
                facts.os_like,
                plan.distro_family if plan else "",
            )
            if item
        ).lower()
        candidates: list[str] = []
        if any(token in raw for token in ("arch", "manjaro", "endeavour")):
            candidates.append("arch")
        if any(token in raw for token in ("ubuntu", "mint", "pop", "zorin")):
            candidates.extend(["ubuntu", "debian"])
        if "debian" in raw and "debian" not in candidates:
            candidates.extend(["debian", "ubuntu"])
        if any(token in raw for token in ("fedora", "rhel", "red hat", "centos", "rocky", "alma")):
            candidates.append("fedora")
        if any(token in raw for token in ("opensuse", "suse")):
            candidates.append("opensuse")
        if "alpine" in raw:
            candidates.append("alpine")
        if "void" in raw:
            candidates.append("void")
        if "gentoo" in raw:
            candidates.append("gentoo")
        return list(dict.fromkeys(candidates))

    @staticmethod
    def _looks_like_flatpak_id(value: str) -> bool:
        return bool(re.fullmatch(r"(?:org|com|io|net|dev|app)\.[A-Za-z0-9_.-]+", value or ""))

    @staticmethod
    def _binary_aliases_from_variants(variants: set[str]) -> set[str]:
        aliases: set[str] = set()
        for variant in variants:
            lowered = variant.lower()
            aliases.add(lowered)
            for suffix in ("-bin", "-git", "-desktop", "-app"):
                if lowered.endswith(suffix):
                    aliases.add(lowered[: -len(suffix)])
        return aliases

    @staticmethod
    def _clean_aliases(values: set[str]) -> set[str]:
        return {str(value or "").strip().lower() for value in values if str(value or "").strip()}

    @staticmethod
    def _clean_executable_aliases(values: set[str]) -> set[str]:
        return {
            str(value or "").strip().lower()
            for value in values
            if re.fullmatch(r"[A-Za-z0-9._+-]+", str(value or "").strip())
            and "/" not in str(value)
            and "." not in str(value).strip().strip(".")
        }

    @staticmethod
    def _drop_weak_binary_desktop_aliases(values: set[str]) -> set[str]:
        return {
            value
            for value in values
            if value not in _WEAK_APP_BINARY_DESKTOP_ALIASES
            and not any(
                value == f"{weak}-bin" or value == f"{weak}-git"
                for weak in _WEAK_APP_BINARY_DESKTOP_ALIASES
            )
        }

    def _match_app_verification_signal(
        self,
        app: AppPlanItem,
        aliases: AppVerificationAliases,
        *,
        native_installed: set[str],
        flatpak_installed: set[str],
        snap_installed: set[str],
        binary_installed: set[str],
        desktop_entries: set[str],
        completed_group_tokens: set[str],
    ) -> AppVerificationMatch | None:
        checks = (
            ("native_package", aliases.native_packages, native_installed),
            ("flatpak", aliases.flatpak_ids, flatpak_installed),
            ("snap", aliases.snap_names, snap_installed),
            ("binary", aliases.binaries, binary_installed),
            ("desktop_entry", aliases.desktop_entries, desktop_entries),
            ("verified_app_group", aliases.native_packages, completed_group_tokens),
        )
        for signal, expected, observed in checks:
            target = self._first_alias_match(
                expected,
                observed,
                allow_compact=signal in {"native_package", "verified_app_group"},
            )
            if not target:
                continue
            alternative = self._app_is_explicit_alternative(app)
            return AppVerificationMatch(
                app_name=app.name,
                signal=signal,
                target=target,
                alternative=alternative,
                alternative_name=self._alternative_display_name(app, target) if alternative else "",
            )
        return None

    @staticmethod
    def _first_alias_match(
        expected: set[str],
        observed: set[str],
        *,
        allow_compact: bool = True,
    ) -> str:
        if not expected or not observed:
            return ""
        expected_lower = {item.lower() for item in expected}
        observed_lower = {item.lower() for item in observed}
        for item in sorted(expected_lower.intersection(observed_lower)):
            return item
        if not allow_compact:
            return ""
        expected_compact = {
            re.sub(r"[^a-z0-9]+", "", item): item for item in sorted(expected_lower)
        }
        observed_compact = {
            re.sub(r"[^a-z0-9]+", "", item): item for item in sorted(observed_lower)
        }
        for compact in sorted(set(expected_compact).intersection(observed_compact)):
            if compact:
                return observed_compact[compact]
        return ""

    def _query_installed_snaps(self) -> set[str]:
        final_state = self.ctx.stage_state.setdefault(SetupStage.FINAL_AUDIT.value, {})
        command = (
            "if command -v snap >/dev/null 2>&1; then "
            "snap list 2>/dev/null | awk 'NR>1 {print $1}'; "
            "fi"
        )
        rc, stdout, stderr = self.runtime.ssh_exec(command, 45)
        self.runlog.record(
            "final_snap_verification",
            command=command,
            rc=rc,
            stdout_tail=(stdout or "")[-800:],
            stderr_tail=(stderr or "")[-800:],
        )
        final_state["snap_verification_rc"] = rc
        final_state["installed_snaps"] = sorted(
            line.strip() for line in (stdout or "").splitlines() if line.strip()
        )
        if rc != 0:
            return set()
        return {line.strip().lower() for line in (stdout or "").splitlines() if line.strip()}

    def _query_installed_binaries(self, aliases: list[str]) -> set[str]:
        final_state = self.ctx.stage_state.setdefault(SetupStage.FINAL_AUDIT.value, {})
        safe_aliases = [alias for alias in dict.fromkeys(aliases) if alias]
        if not safe_aliases:
            return set()
        quoted = " ".join(shlex.quote(alias) for alias in safe_aliases)
        command = (
            f"for qh_bin in {quoted}; do "
            'if command -v "$qh_bin" >/dev/null 2>&1; then '
            "printf '%s\\n' \"$qh_bin\"; "
            "fi; "
            "done"
        )
        rc, stdout, stderr = self.runtime.ssh_exec(command, 45)
        self.runlog.record(
            "final_binary_verification",
            command=command,
            rc=rc,
            stdout_tail=(stdout or "")[-800:],
            stderr_tail=(stderr or "")[-800:],
        )
        final_state["binary_verification_rc"] = rc
        final_state["installed_binaries"] = sorted(
            line.strip() for line in (stdout or "").splitlines() if line.strip()
        )
        if rc != 0:
            return set()
        return {line.strip().lower() for line in (stdout or "").splitlines() if line.strip()}

    def _query_desktop_app_entries(self) -> set[str]:
        final_state = self.ctx.stage_state.setdefault(SetupStage.FINAL_AUDIT.value, {})
        facts = self.ctx.facts or SetupFacts()
        dirs = [
            "/usr/share/applications",
            "/usr/local/share/applications",
            "$HOME/.local/share/applications",
        ]
        login_user = (facts.login_user or facts.current_user or "").strip()
        if login_user and re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", login_user):
            dirs.append(f"/home/{login_user}/.local/share/applications")
        dirs_q = " ".join(shlex.quote(item) for item in dict.fromkeys(dirs))
        command = rf"""for qh_dir in {dirs_q}; do
  eval "qh_expanded=$qh_dir"
  [ -d "$qh_expanded" ] || continue
  find "$qh_expanded" -maxdepth 1 -type f -name '*.desktop' -print 2>/dev/null
done | while IFS= read -r qh_file; do
  qh_id=$(basename "$qh_file" .desktop)
  qh_name=$(awk -F= 'tolower($1)=="name" {{print $2; exit}}' "$qh_file" 2>/dev/null)
  qh_exec=$(awk -F= 'tolower($1)=="exec" {{print $2; exit}}' "$qh_file" 2>/dev/null)
  qh_exec=${{qh_exec%% *}}
  qh_exec=${{qh_exec##*/}}
  printf '%s\t%s\t%s\n' "$qh_id" "$qh_name" "$qh_exec"
done"""
        rc, stdout, stderr = self.runtime.ssh_exec(command, 45)
        self.runlog.record(
            "final_desktop_entry_verification",
            command=command,
            rc=rc,
            stdout_tail=(stdout or "")[-800:],
            stderr_tail=(stderr or "")[-800:],
        )
        aliases: set[str] = set()
        entries: list[str] = []
        if rc == 0:
            for line in (stdout or "").splitlines():
                parts = [part.strip() for part in line.split("\t") if part.strip()]
                if not parts:
                    continue
                entries.append(line.strip())
                for part in parts:
                    aliases.update(self._package_name_variants(part, ""))
        final_state["desktop_entry_verification_rc"] = rc
        final_state["installed_desktop_entries"] = entries
        if rc != 0:
            return set()
        return self._clean_aliases(aliases)

    def _completed_app_group_verification_tokens(self) -> set[str]:
        if self.ctx.plan is None:
            return set()
        tokens: set[str] = set()
        for group in self.ctx.plan.command_groups:
            if group.stage != SetupStage.APPS or group.status != PlanItemStatus.COMPLETED:
                continue
            if not group.verification:
                continue
            tokens.update(self._clean_aliases({*group.packages}))
            for command in [*group.commands, *group.verification]:
                tokens.update(
                    self._clean_aliases(set(self._install_payload_tokens_for_command(command)))
                )
        return tokens

    def _app_is_explicit_alternative(self, app: AppPlanItem) -> bool:
        source = self._normalized_app_source(app.source)
        return source == "alternative" or source.endswith("_alternative")

    @staticmethod
    def _alternative_display_name(app: AppPlanItem, target: str) -> str:
        package = (app.package_name or "").strip()
        if package:
            return package
        return target

    @staticmethod
    def _record_app_verification_manual_followup(
        final_state: dict[str, Any],
        app_names: list[str],
        reason: str,
    ) -> None:
        manual = list(final_state.get("app_verification_manual_followups") or [])
        for app_name in sorted(set(app_names)):
            item = {"app": app_name, "reason": reason}
            if item not in manual:
                manual.append(item)
        final_state["app_verification_manual_followups"] = manual

    def _query_installed_flatpaks(self) -> set[str]:
        final_state = self.ctx.stage_state.setdefault(SetupStage.FINAL_AUDIT.value, {})
        final_state["flatpak_verification_ran"] = True
        facts = self.ctx.facts or SetupFacts()
        login_user = (facts.login_user or facts.current_user or "").strip()
        extra_user_probe = ""
        if login_user and login_user != "root" and login_user != facts.current_user:
            extra_user_probe = (
                f"sudo -u {shlex.quote(login_user)} flatpak list --app "
                "--columns=application --user 2>/dev/null || true; "
            )
        command = (
            "if command -v flatpak >/dev/null 2>&1; then "
            "flatpak list --app --columns=application --user 2>/dev/null || true; "
            f"{extra_user_probe}"
            "flatpak list --app --columns=application --system 2>/dev/null || true; "
            "fi"
        )
        rc, stdout, stderr = self.runtime.ssh_exec(command, 45)
        self.runlog.record(
            "final_flatpak_verification",
            command=command,
            rc=rc,
            stdout_tail=(stdout or "")[-800:],
            stderr_tail=(stderr or "")[-800:],
        )
        final_state["flatpak_verification_rc"] = rc
        final_state["installed_flatpaks"] = sorted(
            line.strip() for line in (stdout or "").splitlines() if line.strip()
        )
        if rc != 0:
            return set()
        return {line.strip().lower() for line in (stdout or "").splitlines() if line.strip()}

    @staticmethod
    def _app_uses_flatpak(app: AppPlanItem) -> bool:
        source = (app.source or "").strip().lower()
        package = (app.package_name or "").strip()
        return source in {"flatpak", "flathub"} or package.lower().startswith(
            ("org.", "com.", "io.", "net.")
        )

    def _flatpak_name_variants(self, app: AppPlanItem) -> set[str]:
        variants = self._package_name_variants(app.name, app.package_name)
        package = (app.package_name or "").strip().lower()
        if package:
            variants.add(package)
        return variants

    @staticmethod
    def _package_name_variants(app_name: str, package_name: str) -> set[str]:
        raw = {app_name, package_name}
        variants: set[str] = set()
        for value in raw:
            normalized = (value or "").strip().lower()
            if not normalized:
                continue
            dashed = normalized.replace(" ", "-")
            compact = normalized.replace(" ", "")
            compact_separators = re.sub(r"[\s_-]+", "", normalized)
            variants.update(
                {
                    normalized,
                    dashed,
                    compact,
                    compact_separators,
                    f"{dashed}-bin",
                    f"{dashed}-git",
                }
            )
            for suffix in ("-bin", "-git"):
                if dashed.endswith(suffix):
                    base = dashed[: -len(suffix)]
                    compact_base = re.sub(r"[\s_-]+", "", base)
                    if compact_base:
                        variants.add(f"{compact_base}{suffix}")
        for key in {app_name.strip().lower(), package_name.strip().lower()}:
            variants.update(_APP_PACKAGE_ALIASES.get(key, set()))
        return {variant for variant in variants if variant}

    @staticmethod
    def _variant_appears_in_command_text(variant: str, text: str) -> bool:
        if not variant or not text:
            return False
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_.+-]){re.escape(variant.lower())}(?![A-Za-z0-9_.+-])"
        )
        return bool(pattern.search(text.lower()))

    def _verify_fstab_root_entry(self) -> list[str]:
        if self.ctx.facts is None or self.ctx.facts.is_live:
            return []
        command = (
            "if [ ! -f /etc/fstab ]; then echo missing-fstab; exit 2; fi; "
            "root_src=$(awk '$2 == \"/\" {print $1; exit}' /etc/fstab); "
            "printf '%s\\n' \"$root_src\"; "
            '[ -n "$root_src" ] || exit 2; '
            "blkid_cmd=$(command -v blkid || command -v /usr/sbin/blkid || "
            "command -v /sbin/blkid || true); "
            '[ -n "$blkid_cmd" ] || exit 6; '
            'case "$root_src" in '
            "UUID=) exit 3 ;; "
            "UUID=*) uuid=${root_src#UUID=}; "
            '[ -n "$uuid" ] && "$blkid_cmd" -U "$uuid" >/dev/null 2>&1 || exit 4 ;; '
            "PARTUUID=*) partuuid=${root_src#PARTUUID=}; "
            '[ -n "$partuuid" ] && "$blkid_cmd" -t PARTUUID="$partuuid" >/dev/null 2>&1 || exit 4 ;; '
            'LABEL=*) label=${root_src#LABEL=}; [ -n "$label" ] || exit 3 ;; '
            '/dev/*) test -b "$root_src" || exit 4 ;; '
            "*) exit 5 ;; "
            "esac"
        )
        rc, stdout, stderr = self._ssh_exec_with_transient_retries(
            command,
            30,
            stage=SetupStage.FINAL_AUDIT,
            group_id="final-fstab-root",
            label="final fstab root verification",
        )
        final_state = self.ctx.stage_state.setdefault(SetupStage.FINAL_AUDIT.value, {})
        final_state["fstab_root_probe"] = {
            "rc": rc,
            "stdout_tail": (stdout or "")[-500:],
            "stderr_tail": (stderr or "")[-500:],
        }
        self.runlog.record(
            "final_fstab_root_verification",
            rc=rc,
            stdout_tail=(stdout or "")[-800:],
            stderr_tail=(stderr or "")[-800:],
        )
        if rc == 0:
            return []
        if self._looks_like_transient_ssh_disconnect(stdout, stderr):
            detail = (stderr or stdout or "SSH transport failed").strip()[:240]
            return [
                "Could not verify root mount source during final audit because SSH transport "
                f"failed: {detail}. Reconnect and rerun final verification before declaring "
                "setup complete."
            ]
        observed = (stdout or stderr or "").strip().splitlines()
        root_src = observed[-1] if observed else "<none>"
        return [
            "Root fstab entry is missing or invalid "
            f"({root_src}). Re-write /etc/fstab with a valid UUID/PARTUUID/LABEL/block "
            "device before declaring setup complete."
        ]

    def _verify_boot_artifacts(self) -> list[str]:
        if self.ctx.facts is None or self.ctx.facts.is_live:
            return []
        install_profile = (getattr(self.profile, "install_profile", "") or "").strip()
        remote_setup_mode = (getattr(self.profile, "remote_setup_mode", "") or "").strip()
        if install_profile == "configure_existing" or remote_setup_mode == "configure_only":
            final_state = self.ctx.stage_state.setdefault(SetupStage.FINAL_AUDIT.value, {})
            final_state["boot_artifact_probe"] = {
                "skipped": True,
                "reason": "configure-existing setup did not perform install or bootloader work",
            }
            self.runlog.record(
                "final_boot_artifact_verification_skipped",
                reason="configure-existing setup did not perform install or bootloader work",
            )
            return []
        command = r"""kernel_count=$(find /boot -maxdepth 3 -type f -name 'vmlinuz*' 2>/dev/null | wc -l)
initramfs_count=$(find /boot -maxdepth 3 -type f \( -name 'initramfs*' -o -name 'initrd*' \) 2>/dev/null | wc -l)
bootloader_count=0
if [ -d /sys/firmware/efi ]; then
  bootloader_count=$(find /boot /boot/efi /efi -maxdepth 6 -type f \( -iname '*.efi' -o -iname 'loader.conf' \) 2>/dev/null | wc -l)
else
  bootloader_count=$(find /boot -maxdepth 4 -type f \( -path '*/grub.cfg' -o -path '*/grubenv' -o -name 'core.img' \) 2>/dev/null | wc -l)
fi
printf 'QH_KERNEL_COUNT=%s\n' "$kernel_count"
printf 'QH_INITRAMFS_COUNT=%s\n' "$initramfs_count"
printf 'QH_BOOTLOADER_COUNT=%s\n' "$bootloader_count"
if [ "$kernel_count" -le 0 ] || [ "$initramfs_count" -le 0 ] || [ "$bootloader_count" -le 0 ]; then
  exit 1
fi
"""
        rc, stdout, stderr = self.runtime.ssh_exec(command, 45)
        final_state = self.ctx.stage_state.setdefault(SetupStage.FINAL_AUDIT.value, {})
        final_state["boot_artifact_probe"] = {
            "rc": rc,
            "stdout_tail": (stdout or "")[-500:],
            "stderr_tail": (stderr or "")[-500:],
        }
        self.runlog.record(
            "final_boot_artifact_verification",
            rc=rc,
            stdout_tail=(stdout or "")[-800:],
            stderr_tail=(stderr or "")[-800:],
        )
        if rc == 0:
            return []
        counts: dict[str, str] = {}
        for line in (stdout or "").splitlines():
            if line.startswith("QH_") and "=" in line:
                key, value = line.split("=", 1)
                counts[key] = value.strip()
        detail = (
            "kernel="
            + counts.get("QH_KERNEL_COUNT", "?")
            + ", initramfs="
            + counts.get("QH_INITRAMFS_COUNT", "?")
            + ", bootloader="
            + counts.get("QH_BOOTLOADER_COUNT", "?")
        )
        return [
            "Bootability artifacts are missing after install "
            f"({detail}). Ensure /boot contains a kernel, initramfs, and bootloader "
            "artifact before declaring setup complete."
        ]

    def _verify_systemd_health(self) -> list[str]:
        if self.ctx.facts is None or self.ctx.facts.init_system != "systemd":
            return []
        command = "systemctl --failed --no-legend --plain 2>/dev/null | awk '{print $1}' | head -20"
        rc, stdout, stderr = self._ssh_exec_with_transient_retries(
            command,
            20,
            stage=SetupStage.FINAL_AUDIT,
            group_id="final-systemd-health",
            label="final systemd health verification",
        )
        self.runlog.record(
            "final_systemd_verification",
            rc=rc,
            stdout_tail=(stdout or "")[-800:],
            stderr_tail=(stderr or "")[-800:],
        )
        if rc != 0:
            return [
                "systemctl --failed could not be queried successfully "
                f"(rc={rc}; {(stderr or stdout or '').strip()[:200]}). "
                "Verify systemd health before declaring setup complete."
            ]
        failed_units = [
            line.strip()
            for line in (stdout or "").splitlines()
            if line.strip() and not is_non_blocking_failed_systemd_unit(line)
        ]
        if not failed_units:
            return []
        return [
            "systemctl reports failed unit(s): "
            + ", ".join(failed_units)
            + ". Investigate them before declaring setup complete."
        ]

    def _verify_display_manager_session_health(self) -> list[str]:
        if self.ctx.facts is None or self.ctx.facts.init_system != "systemd":
            return []
        facts = self.ctx.facts
        if not facts.display_manager_enabled or not facts.display_manager_active:
            return []
        command = (
            "if ! command -v loginctl >/dev/null 2>&1; then exit 0; fi; "
            "found=0; "
            "for s in $(loginctl list-sessions --no-legend 2>/dev/null | awk '{print $1}'); do "
            'remote=$(loginctl show-session "$s" -p Remote --value 2>/dev/null || true); '
            'class=$(loginctl show-session "$s" -p Class --value 2>/dev/null || true); '
            'state=$(loginctl show-session "$s" -p State --value 2>/dev/null || true); '
            'name=$(loginctl show-session "$s" -p Name --value 2>/dev/null || true); '
            'type=$(loginctl show-session "$s" -p Type --value 2>/dev/null || true); '
            'if [ "$remote" != yes ] && '
            "printf '%s\\n' \"$class $state $name $type\" | "
            "grep -Eqi '(greeter|user).*(active|online)|gdm|sddm|lightdm'; then "
            'printf \'%s %s %s %s %s\\n\' "$s" "$name" "$class" "$state" "$type"; '
            "found=1; "
            "fi; "
            "done; "
            'exit $([ "$found" = 1 ] && echo 0 || echo 1)'
        )
        rc, stdout, stderr = self.runtime.ssh_exec(command, 30)
        final_state = self.ctx.stage_state.setdefault(SetupStage.FINAL_AUDIT.value, {})
        final_state["display_manager_session_probe"] = {
            "rc": rc,
            "stdout_tail": (stdout or "")[-500:],
            "stderr_tail": (stderr or "")[-500:],
        }
        self.runlog.record(
            "final_display_manager_session_verification",
            rc=rc,
            stdout_tail=(stdout or "")[-800:],
            stderr_tail=(stderr or "")[-800:],
        )
        if rc == 0:
            return []
        final_state["display_manager_session_status"] = "pending_login_or_headless"
        return []

    def _ask_optional_slow_confirmation(self, group: CommandGroup) -> str | None:
        qid = self._ask_blocking_question(
            f"Install slow optional group '{group.title}' now?",
            choices=["install now", "manual follow-up"],
            stage=group.stage,
            context={"group_id": group.id, "optional_slow": True},
            timeout=300,
            return_qid=False,
        )
        if not qid:
            return None
        if qid.strip().lower() not in {"install now", "yes", "y"}:
            return None
        return qid

    def _ask_blocking_question(
        self,
        question: str,
        *,
        choices: list[str] | None,
        stage: SetupStage,
        context: dict[str, Any],
        timeout: float,
        return_qid: bool = False,
    ) -> str | None:
        pending = self.bridge.add_question(
            question,
            choices,
            blocking=True,
            phase="setup",
            context={"stage": stage.value, **context},
        )
        self.bridge.update_setup_run(
            stage=stage.value,
            status="waiting_for_input",
            waiting_for_input=True,
            active_question_id=pending.id,
            blockers=[question],
        )
        self.runlog.record(
            "blocking_question",
            stage=stage.value,
            question_id=pending.id,
            question=question,
            choices=choices,
        )
        deadline = time.monotonic() + max(0.0, timeout)
        answered = False
        while time.monotonic() < deadline:
            if pending.answered.wait(timeout=min(2.0, max(0.0, deadline - time.monotonic()))):
                answered = True
                break
            if self.bridge.stop_requested:
                break
        if self.bridge.stop_requested and not answered:
            self.bridge.answer_question(pending.id, "[stopped before answer]")
            self.bridge.update_setup_run(
                waiting_for_input=False,
                active_question_id="",
                blockers=[],
                status="stopped",
            )
            self.runlog.record(
                "blocking_question_stopped", stage=stage.value, question_id=pending.id
            )
            raise RuntimeError("Agent stopped by user while waiting for an answer.")
        if not answered:
            self.bridge.answer_question(pending.id, "[timed out waiting for answer]")
            self.bridge.update_setup_run(
                waiting_for_input=False,
                active_question_id="",
                blockers=[],
                status="running",
            )
            self.runlog.record(
                "blocking_question_timeout", stage=stage.value, question_id=pending.id
            )
            return None
        self.bridge.update_setup_run(
            waiting_for_input=False,
            active_question_id="",
            blockers=[],
            status="running",
        )
        self.runlog.record(
            "blocking_question_answer",
            stage=stage.value,
            question_id=pending.id,
            answer=pending.answer,
        )
        return pending.id if return_qid else (pending.answer or "")

    def _execute_group(
        self,
        stage: SetupStage,
        group: CommandGroup,
        stage_state: dict[str, Any],
    ) -> GroupFailure | None:
        group.status = PlanItemStatus.IN_PROGRESS
        stage_state["failed_verifications"] = []
        self.bridge.add_message("agent", f"[{stage.value}] {group.title}")
        self.runlog.record(
            "command_group_started",
            stage=stage.value,
            group=asdict(group),
        )
        operation_commands = self._prepare_group_operations(stage, group, stage_state)
        if operation_commands is None:
            return GroupFailure(
                failure_type="safe_abort",
                command="typed setup operation",
                stdout="",
                stderr="",
                detail="Dangerous operation was safely aborted before execution.",
            )
        commands = [*operation_commands, *self._commands_for_group(stage, group)]
        for command in commands:
            command = self._rewrite_arch_community_helper_command(command, stage=stage, group=group)
            visible_command = command
            if stage == SetupStage.DESKTOP and not self._desktop_runtime_command_ready_or_followup(
                visible_command,
                group,
                stage_state,
                item_kind="command",
            ):
                continue
            if stage == SetupStage.INSTALL_OR_SKIP and self._is_reboot_command(command):
                stage_state["deferred_reboot_command"] = command
                self.runlog.record(
                    "command_deferred_until_guarded_reboot",
                    stage=stage.value,
                    group_id=group.id,
                    command=command,
                )
                continue
            self._grant_known_postboot_driver_command_capability(stage, group, command)
            shadow_preflight = os.environ.get("QUICKHATCH_PREFLIGHT_SHADOW") == "1"
            decisions = preflight_setup_decisions(
                command,
                self.ctx.facts or SetupFacts(),
                shadow_mode=shadow_preflight,
                capability_store=self.ctx.capability_store,
                disk_inventory=self.ctx.last_disk_inventory,
            )
            visible_decisions = [
                decision
                for decision in decisions
                if decision.kind in {PreflightDecisionKind.ADVISORY, PreflightDecisionKind.SHADOW}
            ]
            if visible_decisions:
                advisory_text = "\n".join(
                    f"- [{decision.kind.value}:{decision.category}] {decision.reason}"
                    for decision in visible_decisions
                )
                self.bridge.add_message(
                    "agent",
                    f"[harness] ⚠️ Advisory for `{redact_sensitive_text(command)}`\n{advisory_text}",
                )
                self.runlog.record(
                    "command_preflight_decision",
                    stage=stage.value,
                    group_id=group.id,
                    command=command,
                    decisions=[
                        _preflight_decision_payload(decision) for decision in visible_decisions
                    ],
                )
            if (
                (self.ctx.facts or SetupFacts()).package_manager == "pacman"
                and self._uses_arch_community_helper_command(command)
                and not self._arch_aur_helper_available()
            ):
                bootstrap_failure = self._try_bootstrap_arch_aur_helper_for_command(
                    command,
                    stage=stage,
                    group=group,
                    stage_state=stage_state,
                )
                if bootstrap_failure is not None:
                    return bootstrap_failure
                command = self._rewrite_arch_community_helper_command(
                    command, stage=stage, group=group
                )
                self.runlog.record(
                    "command_preflight_recovered",
                    stage=stage.value,
                    group_id=group.id,
                    command=command,
                    recovery="arch_aur_helper_bootstrap",
                )
            blocking_decisions = [
                decision
                for decision in preflight_setup_decisions(
                    command,
                    self.ctx.facts or SetupFacts(),
                    shadow_mode=shadow_preflight,
                    capability_store=self.ctx.capability_store,
                    disk_inventory=self.ctx.last_disk_inventory,
                )
                if decision.kind == PreflightDecisionKind.BLOCK
            ]
            if blocking_decisions:
                bootstrap_failure = self._try_bootstrap_arch_aur_helper_for_command(
                    command,
                    stage=stage,
                    group=group,
                    stage_state=stage_state,
                )
                if bootstrap_failure is not None:
                    return bootstrap_failure
                command = self._rewrite_arch_community_helper_command(
                    command, stage=stage, group=group
                )
                if self._arch_aur_helper_available() and self._uses_arch_community_helper_command(
                    command
                ):
                    blocking_decisions = [
                        decision
                        for decision in preflight_setup_decisions(
                            command,
                            self.ctx.facts or SetupFacts(),
                            shadow_mode=shadow_preflight,
                            capability_store=self.ctx.capability_store,
                            disk_inventory=self.ctx.last_disk_inventory,
                        )
                        if decision.kind == PreflightDecisionKind.BLOCK
                    ]
                if not blocking_decisions:
                    self.runlog.record(
                        "command_preflight_recovered",
                        stage=stage.value,
                        group_id=group.id,
                        command=command,
                        recovery="arch_aur_helper_bootstrap",
                    )
                else:
                    lint_error = blocking_decisions[0].reason
                    self.bridge.add_message(
                        "agent",
                        f"[harness] 🛑 Safety/precondition block: `{command}`\n{lint_error}",
                    )
                    self.runlog.record(
                        "command_preflight_blocked",
                        stage=stage.value,
                        group_id=group.id,
                        command=command,
                        decision=_preflight_decision_payload(blocking_decisions[0]),
                        reason=lint_error,
                    )
                    return GroupFailure(
                        failure_type="preflight",
                        command=visible_command,
                        stdout="",
                        stderr="",
                        detail=lint_error,
                    )
            command = self._wrap_desktop_runtime_command(command, stage=stage)
            if command != visible_command:
                self.bridge.add_message(
                    "agent",
                    f"`{redact_sensitive_text(visible_command)}` "
                    "— using active GUI session environment",
                )
            else:
                self.bridge.add_message("agent", f"`{redact_sensitive_text(visible_command)}`")
            rc, stdout, stderr = self._ssh_exec_with_transient_retries(
                command,
                self._command_timeout(command),
                stage=stage,
                group_id=group.id,
                label="command",
            )
            self.ctx.results_log.append(
                redact_sensitive_text(f"$ {command}\n[rc={rc}]\n{stdout}\n{stderr}".strip())
            )
            self.ctx.results_log[:] = self.ctx.results_log[-self.runtime.results_log_limit :]
            self.runlog.record(
                "command",
                stage=stage.value,
                group_id=group.id,
                command=command,
                visible_command=visible_command,
                rc=rc,
                stdout_tail=(stdout or "")[-800:],
                stderr_tail=(stderr or "")[-800:],
                failure_classification=(
                    {
                        "category": (
                            failure := classify_failure(
                                command=visible_command,
                                rc=rc,
                                stdout=stdout,
                                stderr=stderr,
                            )
                        ).category.value,
                        "detail": failure.detail,
                        "confidence": failure.confidence,
                    }
                    if rc != 0
                    else None
                ),
            )
            if rc != 0:
                signature = build_attempt_signature(
                    stage=stage.value,
                    group_id=group.id,
                    group_title=group.title,
                    command=visible_command,
                    stdout=stdout,
                    stderr=stderr,
                    detail=stderr.strip() or stdout.strip(),
                )
                self.runlog.record(
                    "attempt_signature",
                    stage=stage.value,
                    group_id=group.id,
                    command=visible_command,
                    signature=asdict(signature),
                    diagnostic=is_diagnostic_command(visible_command),
                )
                if self._looks_like_live_installer_reboot_disconnect(
                    stage, visible_command, stdout, stderr
                ):
                    stage_state["installer_reboot_observed"] = True
                    created_user = self._extract_created_user(visible_command)
                    if created_user:
                        self.ctx.created_user = created_user
                    self.runlog.record(
                        "live_installer_reboot_observed",
                        stage=stage.value,
                        group_id=group.id,
                        command=visible_command,
                        rc=rc,
                        stdout_tail=(stdout or "")[-500:],
                        stderr_tail=(stderr or "")[-500:],
                    )
                    self.bridge.add_message(
                        "agent",
                        "Live installer closed the SSH session; assuming it is rebooting "
                        "into the installed system instead of retrying the installer command.",
                    )
                    return None
                if self._looks_like_live_disk_reread_required(
                    stage, visible_command, stdout, stderr
                ):
                    detail = self._live_disk_reread_required_message(visible_command)
                    stage_state["live_disk_reboot_required"] = True
                    self.runlog.record(
                        "live_disk_reboot_required",
                        stage=stage.value,
                        group_id=group.id,
                        command=visible_command,
                        rc=rc,
                        stdout_tail=(stdout or "")[-500:],
                        stderr_tail=(stderr or "")[-500:],
                    )
                    self.bridge.add_message("agent", f"⚠️ {detail}")
                    return GroupFailure(
                        failure_type="live_reboot_required",
                        command=visible_command,
                        stdout=stdout,
                        stderr=stderr,
                        detail=detail,
                    )
                stage_state.setdefault("failed_commands", []).append(command)
                detail = stderr.strip() or stdout.strip() or "command failed"
                self.bridge.add_message(
                    "agent",
                    self._format_group_failure_message(
                        group.title,
                        "Command failed",
                        visible_command,
                        rc,
                        stdout,
                        stderr,
                    ),
                )
                return GroupFailure(
                    failure_type="command",
                    command=visible_command,
                    stdout=stdout,
                    stderr=stderr,
                    detail=detail,
                )
            self._consume_capabilities_for_command(command, stage=stage, group=group)
            self._update_from_command(visible_command, stdout, stderr, stage=stage)
        try:
            self._maybe_reboot_for_nvidia_driver(stage, group, stage_state)
        except RuntimeError as exc:
            detail = str(exc) or "NVIDIA driver reboot/reconnect failed."
            self.bridge.add_message(
                "agent",
                f"⚠️ NVIDIA reboot/reconnect step failed in '{group.title}': {detail}",
            )
            return GroupFailure(
                failure_type="nvidia_reboot",
                command="controlled NVIDIA driver reboot",
                stdout="",
                stderr=detail,
                detail=detail,
            )
        for verification in group.verification:
            verification = self._normalize_mountpoint_probe_command(verification)
            verification = self._normalize_live_nonroot_chroot_command(verification, stage=stage)
            verification = self._normalize_live_nonroot_target_path_verification(
                verification, stage=stage
            )
            verification = self._normalize_installed_nonroot_privileged_command(
                verification, stage=stage
            )
            verification = self._normalize_package_verification_command(
                verification, stage=stage, group=group
            )
            normalized_verification = self._normalize_install_root_fstab_verification(
                verification, stage=stage
            )
            if normalized_verification != verification:
                self.runlog.record(
                    "verification_normalized",
                    stage=stage.value,
                    group_id=group.id,
                    original=verification,
                    normalized=normalized_verification,
                    reason="install-stage fstab root checks must be field-aware and target-root scoped",
                )
                verification = normalized_verification
            normalized_verification = self._normalize_preboot_driver_runtime_verification(
                verification, stage=stage
            )
            if normalized_verification != verification:
                self.runlog.record(
                    "verification_normalized",
                    stage=stage.value,
                    group_id=group.id,
                    original=verification,
                    normalized=normalized_verification,
                    reason="driver runtime/module probes are postboot checks, not live-install bootability checks",
                )
                verification = normalized_verification
            normalized_verification = self._normalize_sysfs_module_parameter_verification(
                verification, stage=stage
            )
            if normalized_verification != verification:
                self.runlog.record(
                    "verification_normalized",
                    stage=stage.value,
                    group_id=group.id,
                    original=verification,
                    normalized=normalized_verification,
                    reason="kernel module parameter files may be root-readable only",
                )
                verification = normalized_verification
            normalized_verification = self._normalize_pre_format_partition_verification(
                verification, group=group
            )
            if normalized_verification != verification:
                self.runlog.record(
                    "verification_normalized",
                    stage=stage.value,
                    group_id=group.id,
                    original=verification,
                    normalized=normalized_verification,
                    reason="partition groups should verify partition nodes before mkfs, not filesystem labels",
                )
                verification = normalized_verification
            normalized_verification = self._normalize_fstab_mount_verification(verification)
            if normalized_verification != verification:
                self.runlog.record(
                    "verification_normalized",
                    stage=stage.value,
                    group_id=group.id,
                    original=verification,
                    normalized=normalized_verification,
                    reason="fstab mount checks must match fields, not whitespace-sensitive text",
                )
                verification = normalized_verification
            normalized_verification = self._normalize_desktop_display_manager_verification(
                verification,
                stage=stage,
            )
            if normalized_verification != verification:
                self.runlog.record(
                    "verification_normalized",
                    stage=stage.value,
                    group_id=group.id,
                    original=verification,
                    normalized=normalized_verification,
                    reason=(
                        "desktop display manager install groups should accept an enabled "
                        "manager before the final controlled start/poll step"
                    ),
                )
                verification = normalized_verification
            if stage == SetupStage.DESKTOP and not self._desktop_runtime_command_ready_or_followup(
                verification,
                group,
                stage_state,
                item_kind="verification",
            ):
                continue
            self.bridge.add_message(
                "agent",
                f"`{redact_sensitive_text(verification)}` — verify",
            )
            rc, stdout, stderr = self._ssh_exec_with_transient_retries(
                verification,
                120,
                stage=stage,
                group_id=group.id,
                label="verification",
            )
            self.runlog.record(
                "verification",
                stage=stage.value,
                group_id=group.id,
                command=verification,
                rc=rc,
                stdout_tail=(stdout or "")[-500:],
                stderr_tail=(stderr or "")[-500:],
            )
            if rc != 0:
                if self._soft_fail_desktop_cosmetic_verification(
                    stage=stage,
                    group=group,
                    verification=verification,
                    rc=rc,
                    stdout=stdout,
                    stderr=stderr,
                    stage_state=stage_state,
                ):
                    continue
                detail = (
                    f"Verification failed for {group.title}: {verification} "
                    f"(rc={rc}; {(stderr or stdout or '').strip()[:200]})"
                )
                stage_state.setdefault("failed_verifications", []).append(detail)
                self.bridge.add_message(
                    "agent",
                    self._format_group_failure_message(
                        group.title,
                        "Verification failed",
                        verification,
                        rc,
                        stdout,
                        stderr,
                    ),
                )
                return GroupFailure(
                    failure_type="verification",
                    command=verification,
                    stdout=stdout,
                    stderr=stderr,
                    detail=detail,
                )
        return None

    def _prepare_group_operations(
        self,
        stage: SetupStage,
        group: CommandGroup,
        stage_state: dict[str, Any],
    ) -> list[str] | None:
        del stage_state
        if not group.operations:
            return []
        facts = self.ctx.facts or SetupFacts()
        family = facts.distro_family or facts.os_id or self.runtime.target_distro_family
        installer = get_family_installer(family)
        commands: list[str] = []
        for operation in group.operations:
            try:
                rendered = installer.render_operation(operation, facts, self.profile)
            except Exception as exc:
                self._safe_abort(
                    stage=stage,
                    reason=f"Could not render typed operation {operation.kind}: {exc}",
                    group=group,
                )
                return None
            capability_kind = operation.capability_kind
            capability_target = (
                operation.mountpoint
                if operation.kind == "mount_partition"
                else operation.target or operation.mountpoint
            )
            canonical_target = canonicalize_target(
                capability_target,
                inventory=self.ctx.last_disk_inventory,
            )
            if capability_kind:
                if not self._authorize_operation(
                    stage=stage,
                    group=group,
                    operation=operation,
                    rendered=rendered,
                    capability_kind=capability_kind,
                    canonical_target=canonical_target,
                ):
                    return None
            self.runlog.record(
                "operation_rendered",
                stage=stage.value,
                group_id=group.id,
                operation=asdict(operation),
                rendered_commands=rendered,
                capability_kind=capability_kind,
            )
            commands.extend(rendered)
        return commands

    def _authorize_operation(
        self,
        *,
        stage: SetupStage,
        group: CommandGroup,
        operation: Any,
        rendered: list[str],
        capability_kind: str,
        canonical_target: str,
    ) -> bool:
        store = self.ctx.capability_store
        if store is None:
            return False
        disk_inventory = self.ctx.last_disk_inventory
        if operation.kind in {"partition_disk", "format_partition"}:
            disk_inventory = self._refresh_disk_inventory(stage=stage, group_id=group.id)
            if getattr(self.profile, "install_profile", "auto") == "dual_boot_free_space":
                reason = dual_boot_blocks_operation(operation, disk_inventory)
                if reason:
                    self._safe_abort(stage=stage, group=group, reason=reason)
                    return False
        existing = store.allows(
            kind=capability_kind,
            target=canonical_target,
            disk_inventory=disk_inventory,
        )
        if existing:
            self.runlog.record(
                "capability_reused",
                stage=stage.value,
                group_id=group.id,
                capability_id=existing.id,
                operation=asdict(operation),
            )
            return True
        preview = build_operation_preview(
            operation=operation,
            rendered_commands=rendered,
            install_profile=getattr(self.profile, "install_profile", "auto") or "auto",
            canonical_target=canonical_target,
            disk_inventory=disk_inventory,
        )
        self.runlog.record(
            "dangerous_operation_preview",
            stage=stage.value,
            group_id=group.id,
            preview=asdict(preview),
        )
        preview_text = self._format_operation_preview(preview)
        self.bridge.add_message("agent", preview_text)
        answer = self._ask_blocking_question(
            (
                "Confirm the dangerous setup operation exactly as shown.\n\n"
                f"Required token: `{preview.confirmation_token}`"
            ),
            choices=[preview.confirmation_token, "skip/manual follow-up"],
            stage=stage,
            context={
                "group_id": group.id,
                "operation": asdict(operation),
                "preview_id": preview.preview_id,
            },
            timeout=1800,
        )
        if (answer or "").strip() != preview.confirmation_token:
            self._safe_abort(
                stage=stage,
                group=group,
                reason="Dangerous operation was not confirmed with the required token.",
            )
            return False
        grant = store.grant(
            kind=capability_kind,
            target=canonical_target,
            confirmation_text=answer,
            confirmation_id=preview.preview_id,
            reason=f"User confirmed {operation.kind} for {canonical_target}",
            disk_inventory=disk_inventory,
            preview_id=preview.preview_id,
        )
        self.runlog.record(
            "capability_granted",
            stage=stage.value,
            group_id=group.id,
            capability=asdict(grant),
        )
        return True

    def _refresh_disk_inventory(self, *, stage: SetupStage, group_id: str) -> str:
        rc, stdout, stderr = self.runtime.ssh_exec(DISK_INVENTORY_COMMAND, 60)
        inventory = stdout.strip()
        self.ctx.last_disk_inventory = inventory
        self.runlog.record(
            "disk_inventory",
            stage=stage.value,
            group_id=group_id,
            command=DISK_INVENTORY_COMMAND,
            rc=rc,
            stdout_tail=(stdout or "")[-2000:],
            stderr_tail=(stderr or "")[-500:],
        )
        return inventory

    def _internal_capability_allows(
        self,
        *,
        kind: str,
        target: str,
        stage: SetupStage,
        group_id: str,
        reason: str,
    ) -> bool:
        store = self.ctx.capability_store
        allowed = bool(
            store
            and store.allows(
                kind=kind,
                target=target,
                disk_inventory=self.ctx.last_disk_inventory,
            )
        )
        self.runlog.record(
            "internal_capability_check",
            stage=stage.value,
            group_id=group_id,
            capability_kind=kind,
            target=target,
            allowed=allowed,
            reason=reason,
        )
        if allowed:
            return True
        self._safe_abort(
            stage=stage,
            group=None,
            reason=(
                f"Internal {reason} needs confirmed QuickHatch capability "
                f"{kind} target={target or '<none>'}."
            ),
        )
        return False

    @staticmethod
    def _format_operation_preview(preview: Any) -> str:
        commands = "\n".join(
            f"- `{redact_sensitive_text(cmd)}`" for cmd in preview.rendered_commands
        )
        inventory = (
            "\n\nDisk inventory:\n```text\n" + preview.disk_inventory.strip()[:4000] + "\n```"
            if preview.disk_inventory
            else ""
        )
        return (
            "[harness] Dangerous-operation preview\n"
            f"- operation: `{preview.operation.get('kind')}`\n"
            f"- target: `{preview.operation.get('target') or preview.operation.get('mountpoint')}`\n"
            f"- install profile: `{preview.install_profile}`\n"
            f"- erases data: `{preview.erases_data}`\n"
            f"- one-time capability: `{preview.one_time_use}`\n"
            f"- required confirmation token: `{preview.confirmation_token}`\n"
            "Rendered command(s):\n"
            f"{commands}"
            f"{inventory}"
        )

    def _safe_abort(self, *, stage: SetupStage, group: CommandGroup | None, reason: str) -> None:
        facts_payload = asdict(self.ctx.facts or SetupFacts())
        self.ctx.stage_state.setdefault(stage.value, {})["safe_abort"] = reason
        self.runlog.record(
            "safe_abort",
            stage=stage.value,
            group_id=group.id if group else "",
            reason=reason,
            facts=facts_payload,
            disk_inventory=self.ctx.last_disk_inventory,
        )
        self.bridge.add_message(
            "agent",
            (
                "🛑 Safe abort: "
                + reason
                + "\nNo further destructive setup operations will run until this is resolved."
            ),
        )

    def _consume_capabilities_for_command(
        self, command: str, *, stage: SetupStage, group: CommandGroup
    ) -> None:
        store = self.ctx.capability_store
        if store is None:
            return
        for requirement in dangerous_command_requirements(command):
            grant = store.allows(
                kind=requirement["kind"],
                target=requirement.get("target", ""),
                disk_inventory=self.ctx.last_disk_inventory,
            )
            if not grant:
                continue
            store.consume(grant.id)
            self.runlog.record(
                "capability_consumed",
                stage=stage.value,
                group_id=group.id,
                command=command,
                capability_id=grant.id,
                kind=grant.kind,
                target=grant.target,
                consumed=grant.consumed,
            )

    def _normalize_sysfs_module_parameter_verification(
        self, command: str, *, stage: SetupStage
    ) -> str:
        facts = self.ctx.facts or SetupFacts()
        if (
            stage not in _INSTALLED_NONROOT_PRIVILEGED_STAGES
            or facts.is_live
            or facts.is_root
            or self._has_privileged_command_wrapper(command.strip())
        ):
            return command
        match = _SYSFS_MODULE_PARAMETER_CAT_RE.match(command or "")
        if not match:
            return command
        return (
            f"{match.group('prefix')}sudo cat "
            f"{shlex.quote(match.group('path'))}{match.group('rest')}"
        )

    def _normalize_install_root_fstab_verification(self, command: str, *, stage: SetupStage) -> str:
        facts = self.ctx.facts or SetupFacts()
        lowered = (command or "").lower()
        if stage != SetupStage.INSTALL_OR_SKIP or not facts.is_live:
            return command
        if "/mnt/etc/fstab" not in lowered:
            return command
        if not any(marker in lowered for marker in ("grep", "awk", "findmnt", " / ")):
            return command
        return _INSTALL_ROOT_FSTAB_VERIFY_COMMAND

    def _normalize_preboot_driver_runtime_verification(
        self, command: str, *, stage: SetupStage
    ) -> str:
        facts = self.ctx.facts or SetupFacts()
        if stage != SetupStage.INSTALL_OR_SKIP or not facts.is_live:
            return command
        if not _PREBOOT_DRIVER_RUNTIME_VERIFY_RE.search(command or ""):
            return command
        return "true"

    @staticmethod
    def _normalize_pre_format_partition_verification(command: str, *, group: CommandGroup) -> str:
        if any("mkfs" in planned.lower() for planned in group.commands):
            return command
        match = _PARTED_PRE_FORMAT_FS_CHECK_RE.search(command or "")
        if not match:
            return command
        disk = match.group("disk").strip("'\"")
        partition = match.group("partition")
        suffix = "p" if disk and disk[-1].isdigit() else ""
        return f"test -b {shlex.quote(disk + suffix + partition)}"

    @staticmethod
    def _normalize_fstab_mount_verification(command: str) -> str:
        """Replace brittle `grep ' / ' fstab` checks with field-aware checks."""

        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            return command
        tool = (tokens[0] or "").strip().split("/")[-1].lower()
        if len(tokens) < 3 or tool != "grep":
            return command
        args = tokens[1:]
        if args and args[0] == "-q":
            args = args[1:]
        if len(args) != 2:
            return command
        mountpoint = args[0].strip()
        path = args[1]
        if mountpoint not in {"/", "/boot"} or not path.endswith("/etc/fstab"):
            return command
        return (
            "awk "
            + shlex.quote(f'$2 == "{mountpoint}" {{ found=1 }} END {{ exit(found ? 0 : 1) }}')
            + " "
            + shlex.quote(path)
        )

    @staticmethod
    def _normalize_desktop_display_manager_verification(command: str, *, stage: SetupStage) -> str:
        if stage != SetupStage.DESKTOP:
            return command
        stripped = (command or "").strip()
        if not stripped:
            return command
        try:
            tokens = shlex.split(stripped, posix=True)
        except ValueError:
            return command
        tokens = SetupOrchestrator._strip_command_wrappers(tokens)
        if len(tokens) < 3 or tokens[0] != "systemctl" or tokens[1] != "is-active":
            return command
        units = [
            token
            for token in tokens[2:]
            if token != "--quiet"
            and not token.startswith("-")
            and not token.startswith(">")
            and token not in {"2>&1", "1>/dev/null"}
        ]
        if len(units) != 1:
            return command
        unit = units[0]
        normalized_unit = unit.removesuffix(".service")
        if normalized_unit not in {"display-manager", "sddm", "gdm", "lightdm", "greetd", "ly"}:
            return command
        quoted = shlex.quote(unit)
        return (
            f"(systemctl is-active {quoted} >/dev/null 2>&1 || "
            f"systemctl is-enabled {quoted} >/dev/null 2>&1 || "
            "systemctl is-enabled display-manager >/dev/null 2>&1)"
        )

    def _synthesize_missing_app_command_groups(self) -> None:
        """Turn resolved app plan rows into executable app groups if the model omitted them."""

        if self.ctx.plan is None or self.ctx.facts is None:
            return
        pending = [
            app
            for app in self.ctx.plan.apps
            if app.status
            in {
                PlanItemStatus.PENDING,
                PlanItemStatus.IN_PROGRESS,
                PlanItemStatus.OPTIONAL_SLOW_PENDING_CONFIRMATION,
            }
            and not self._app_is_covered_by_app_group(app)
        ]
        if not pending:
            return

        repo_packages: list[str] = []
        community_packages: list[str] = []
        flatpak_packages: list[str] = []
        repo_app_names: list[str] = []
        community_app_names: list[str] = []
        flatpak_app_names: list[str] = []
        for app in pending:
            source = self._normalized_app_source(app.source)
            package_name = (app.package_name or "").strip()
            if app.status == PlanItemStatus.OPTIONAL_SLOW_PENDING_CONFIRMATION:
                # Optional slow apps need an explicit user decision. Without a
                # planned group there is nothing safe to ask/execute, so keep
                # setup moving with a visible manual follow-up instead.
                app.status = PlanItemStatus.MANUAL_FOLLOWUP
                app.reason = app.reason or "Optional slow app was left for manual follow-up."
                self._record_optional_slow_apps_manual_followup([app.name])
                continue
            if source == "repo" and package_name:
                repo_packages.append(package_name)
                repo_app_names.append(app.name)
                continue
            if source == "community" and package_name:
                if self.ctx.facts.package_manager == "pacman":
                    community_packages.append(package_name)
                    community_app_names.append(app.name)
                    continue
                app.status = PlanItemStatus.MANUAL_FOLLOWUP
                app.reason = (
                    app.reason
                    or "Community package source is not supported automatically for this distro."
                )
                continue
            if source in {"flatpak", "flathub"} and package_name:
                flatpak_packages.append(package_name)
                flatpak_app_names.append(app.name)
                continue
            app.status = PlanItemStatus.MANUAL_FOLLOWUP
            app.reason = app.reason or "Planner did not provide a resolvable package source."

        groups: list[CommandGroup] = []
        if repo_packages:
            command = build_install_command(repo_packages, "repo", self.ctx.facts)
            if command:
                groups.append(
                    CommandGroup(
                        id="quickhatch-synth-repo-apps",
                        stage=SetupStage.APPS,
                        title="Install resolved repository applications",
                        commands=[command],
                        verification=[
                            self._package_verification_command(repo_packages, self.ctx.facts)
                        ],
                        rationale=(
                            "QuickHatch synthesized this group because the planner resolved "
                            "required repository apps but omitted an executable apps command group."
                        ),
                        source="repo",
                        packages=repo_packages,
                    )
                )
            else:
                self._mark_apps_manual_followup_by_names(
                    repo_app_names,
                    "No typed repository install command is available for this package manager.",
                )
        if community_packages:
            command = self._community_install_command(community_packages, self.ctx.facts)
            if command:
                groups.append(
                    CommandGroup(
                        id="quickhatch-synth-community-apps",
                        stage=SetupStage.APPS,
                        title="Install resolved community applications",
                        commands=[command],
                        verification=[
                            self._package_verification_command(community_packages, self.ctx.facts)
                        ],
                        rationale=(
                            "QuickHatch synthesized this group because the planner resolved "
                            "required community apps but omitted an executable apps command group."
                        ),
                        source="community",
                        packages=community_packages,
                    )
                )
            else:
                self._mark_apps_manual_followup_by_names(
                    community_app_names,
                    "No supported community-package helper is available for this target.",
                )
        if flatpak_packages:
            command = self._flatpak_install_command(flatpak_packages)
            if command:
                groups.append(
                    CommandGroup(
                        id="quickhatch-synth-flatpak-apps",
                        stage=SetupStage.APPS,
                        title="Install resolved Flatpak applications",
                        commands=[command],
                        verification=[self._flatpak_verification_command(flatpak_packages)],
                        rationale=(
                            "QuickHatch synthesized this group because the planner resolved "
                            "required Flatpak apps but omitted an executable apps command group."
                        ),
                        source="flatpak",
                        packages=flatpak_packages,
                    )
                )
            else:
                self._mark_apps_manual_followup_by_names(
                    flatpak_app_names,
                    "No typed Flatpak install command is available for these app identifiers.",
                )
        if not groups:
            return
        self.ctx.plan.command_groups.extend(groups)
        self.bridge.add_message(
            "agent",
            "[harness] Added missing app install group(s) from the validated app plan: "
            + ", ".join(group.title for group in groups),
        )
        self.runlog.record(
            "synthesized_app_command_groups",
            stage=SetupStage.APPS.value,
            groups=[asdict(group) for group in groups],
        )

    def _resolve_requested_apps_from_catalog(self) -> None:
        """Promote selected app rows when the catalog has a family-specific package hint.

        The planner may conservatively backfill omitted requested apps as
        manual_followup. That is correct when package truth is unknown, but it
        should not suppress an executable app stage when QuickHatch already has
        an active distro-family hint for the selected app.
        """

        if self.ctx.plan is None or self.ctx.facts is None:
            return
        requested_names = [
            str(name or "").strip()
            for name in (getattr(self.profile, "apps_to_install", None) or [])
            if str(name or "").strip()
        ]
        if not requested_names:
            return
        distro_family = (self.ctx.facts.distro_family or "").strip() or (
            self.ctx.plan.distro_family or ""
        ).strip()
        rows = selected_app_catalog_context(
            requested_names,
            getattr(self.profile, "apps", None),
            distro_family=distro_family,
        )
        if not rows:
            return

        requested_keys = {self._app_catalog_key(name) for name in requested_names}
        rows_by_key: dict[str, dict[str, Any]] = {}
        for row in rows:
            row_values: list[Any] = [row.get("name"), row.get("canonical_name")]
            row_values.extend(row.get("alternatives") or [])
            for value in row_values:
                key = self._app_catalog_key(value)
                if key:
                    rows_by_key.setdefault(key, row)

        resolved: list[str] = []
        existing_keys: set[str] = set()
        for app in self.ctx.plan.apps:
            app_key = self._app_catalog_key(app.name)
            if app_key:
                existing_keys.add(app_key)
            row = rows_by_key.get(app_key)
            if not row:
                continue
            row_keys = [
                self._app_catalog_key(value)
                for value in [
                    row.get("name"),
                    row.get("canonical_name"),
                    *(row.get("alternatives") or []),
                ]
            ]
            existing_keys.update(key for key in row_keys if key)
        for app in self.ctx.plan.apps:
            app_key = self._app_catalog_key(app.name)
            row = rows_by_key.get(app_key)
            row_requested_key = self._app_catalog_key(row.get("name") if row else "")
            if app_key not in requested_keys and row_requested_key not in requested_keys:
                continue
            if app.optional_slow:
                continue
            if app.status in {
                PlanItemStatus.INSTALLED,
                PlanItemStatus.COMPLETED,
                PlanItemStatus.EXPLICITLY_SKIPPED,
            }:
                continue
            if (
                app.status in {PlanItemStatus.PENDING, PlanItemStatus.IN_PROGRESS}
                and app.package_name
                and self._normalized_app_source(app.source) in {"repo", "community"}
            ):
                continue
            hint = self._active_catalog_package_hint(row)
            if hint is None:
                continue
            source, packages = hint
            app.source = source
            app.package_name = packages[0]
            app.status = PlanItemStatus.PENDING
            app.required = True
            app.reason = ""
            resolved.append(f"{app.name} -> {packages[0]} ({source})")

        for row in rows:
            requested_key = self._app_catalog_key(row.get("name"))
            canonical_key = self._app_catalog_key(row.get("canonical_name"))
            if not requested_key or requested_key not in requested_keys:
                continue
            if requested_key in existing_keys or canonical_key in existing_keys:
                continue
            hint = self._active_catalog_package_hint(row)
            if hint is None:
                continue
            source, packages = hint
            app_name = str(row.get("name") or row.get("canonical_name") or "").strip()
            if not app_name:
                continue
            self.ctx.plan.apps.append(
                AppPlanItem(
                    name=app_name,
                    package_name=packages[0],
                    source=source,
                    status=PlanItemStatus.PENDING,
                    required=True,
                )
            )
            existing_keys.update({requested_key, canonical_key})
            resolved.append(f"{app_name} -> {packages[0]} ({source})")

        if not resolved:
            return
        stage_state = self.ctx.stage_state.setdefault(SetupStage.APPS.value, {})
        stage_state["catalog_resolved_requested_apps"] = sorted(resolved)
        self.bridge.add_message(
            "agent",
            "[harness] Resolved selected app(s) from the distro app catalog: "
            + ", ".join(sorted(resolved)),
        )
        self.runlog.record(
            "catalog_resolved_requested_apps",
            stage=SetupStage.APPS.value,
            apps=sorted(resolved),
            distro_family=distro_family,
        )

    def _active_catalog_package_hint(
        self,
        row: dict[str, Any] | None,
    ) -> tuple[str, list[str]] | None:
        if not row:
            return None
        compatibility = str(row.get("compatibility") or "").strip().lower()
        if compatibility and compatibility != "native":
            return None
        hint = row.get("active_install_hint")
        if not isinstance(hint, dict):
            return None
        source = self._normalized_app_source(str(hint.get("source") or ""))
        if source not in {"repo", "community", "flatpak", "flathub"}:
            return None
        packages = [
            str(package or "").strip()
            for package in (hint.get("packages") or [])
            if str(package or "").strip()
        ]
        if not packages:
            return None
        return source, packages

    def _app_is_covered_by_app_group(self, app: AppPlanItem) -> bool:
        if self.ctx.plan is None:
            return False
        variants = self._package_name_variants(app.name, app.package_name)
        for group in self.ctx.plan.command_groups:
            if group.stage != SetupStage.APPS:
                continue
            group_packages = {pkg.lower() for pkg in group.packages}
            if variants and variants.intersection(group_packages):
                return True
            command_text = " ".join([group.title, *group.commands, *group.verification])
            if any(
                self._variant_appears_in_command_text(variant, command_text) for variant in variants
            ):
                return True
        return False

    @staticmethod
    def _app_catalog_key(value: Any) -> str:
        text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
        return re.sub(r"\s+", " ", text)

    @staticmethod
    def _normalized_app_source(source: str) -> str:
        normalized = (source or "").strip().lower().replace(" ", "_")
        if normalized in _REPO_APP_SOURCES:
            return "repo"
        if normalized in _COMMUNITY_APP_SOURCES:
            return "community"
        return normalized or "manual_followup"

    @staticmethod
    def _community_install_command(packages: list[str], facts: SetupFacts) -> str:
        if facts.package_manager != "pacman":
            return ""
        helper = facts.community_helper if facts.community_helper in {"yay", "paru"} else "yay"
        quoted = " ".join(shlex.quote(package) for package in packages if package)
        if not quoted:
            return ""
        if facts.is_root and facts.login_user and facts.login_user != "root":
            return f"sudo -u {shlex.quote(facts.login_user)} {helper} -S --noconfirm {quoted}"
        return f"{helper} -S --noconfirm {quoted}"

    @staticmethod
    def _flatpak_install_command(packages: list[str]) -> str:
        quoted = " ".join(shlex.quote(package) for package in packages if package)
        if not quoted:
            return ""
        return f"flatpak install -y flathub {quoted}"

    @staticmethod
    def _flatpak_verification_command(packages: list[str]) -> str:
        checks = [
            f"flatpak list --app --columns=application | grep -Fx {shlex.quote(package)}"
            for package in packages
            if package
        ]
        return " && ".join(checks) if checks else "true"

    @staticmethod
    def _package_verification_command(packages: list[str], facts: SetupFacts) -> str:
        quoted = " ".join(shlex.quote(package) for package in packages if package)
        if not quoted:
            return "true"
        pm = facts.package_manager
        if pm == "pacman":
            return f"pacman -Q {quoted}"
        if pm == "apt":
            return " && ".join(
                (
                    f"(dpkg-query -W -f='${{db:Status-Abbrev}}\\n' {shlex.quote(package)} "
                    "2>/dev/null | grep -qx 'ii ' "
                    f"|| snap list {shlex.quote(package)} >/dev/null 2>&1)"
                )
                for package in packages
                if package
            )
        if pm in {"dnf", "zypper"}:
            return f"rpm -q {quoted}"
        if pm == "apk":
            return f"apk info -e {quoted}"
        if pm == "xbps":
            return " && ".join(f"xbps-query {shlex.quote(package)}" for package in packages)
        if pm == "emerge":
            return " && ".join(f"qlist -IC {shlex.quote(package)}" for package in packages)
        return "true"

    def _filter_installed_app_group(
        self, group: CommandGroup, stage_state: dict[str, Any]
    ) -> CommandGroup | None:
        if self.ctx.facts is None:
            return group
        planned_packages = self._app_group_planned_packages(group)
        if not planned_packages:
            return group
        query_packages = self._app_group_package_query_candidates(group)
        installed = self._query_installed_native_packages(query_packages, self.ctx.facts)
        if not installed:
            return group
        installed_in_group = {
            package
            for package in planned_packages
            if self._planned_package_is_installed(package, installed, group)
        }
        if not installed_in_group:
            return group
        missing = [
            package
            for package in planned_packages
            if not self._planned_package_is_installed(package, installed, group)
        ]
        rewritten_commands: list[str] = []
        changed = False
        for command in group.commands:
            rewritten = self._drop_installed_targets_from_app_install_command(
                command, installed_in_group, self.ctx.facts
            )
            if rewritten is None:
                rewritten_commands.append(command)
                continue
            changed = True
            if rewritten:
                rewritten_commands.append(rewritten)
        stage_state.setdefault("already_installed_packages", []).extend(sorted(installed_in_group))
        self.runlog.record(
            "app_group_installed_package_prefilter",
            stage=SetupStage.APPS.value,
            group_id=group.id,
            installed=sorted(installed_in_group),
            missing=missing,
            original_commands=list(group.commands),
            rewritten_commands=rewritten_commands,
        )
        self._mark_apps_installed_by_prefilter(installed_in_group, installed, group)
        if not changed:
            if not missing:
                self.bridge.add_message(
                    "agent",
                    "[harness] App group already installed; skipping package install command(s): "
                    + ", ".join(sorted(installed_in_group)),
                )
                return None
            replacement = build_install_command(missing, group.source, self.ctx.facts)
            if replacement:
                rewritten_commands = [replacement]
                changed = True
            else:
                return group
        if not rewritten_commands:
            self.bridge.add_message(
                "agent",
                "[harness] App group already installed; skipping package install command(s): "
                + ", ".join(sorted(installed_in_group)),
            )
            return None
        if missing:
            self.bridge.add_message(
                "agent",
                "[harness] App package precheck found already-installed package(s): "
                + ", ".join(sorted(installed_in_group))
                + ". Installing only missing package(s): "
                + ", ".join(missing),
            )
        repaired = copy.deepcopy(group)
        repaired.id = f"{group.id}-missing-only"
        repaired.title = f"{group.title} (missing packages only)"
        repaired.commands = rewritten_commands
        repaired.verification = []
        repaired.packages = missing
        return repaired

    def _app_group_planned_packages(self, group: CommandGroup) -> list[str]:
        packages: list[str] = []
        for package in group.packages:
            if package and package not in packages:
                packages.append(package)
        for command in group.commands:
            for package in self._install_payload_tokens_for_command(command):
                if package and package not in packages:
                    packages.append(package)
        return packages

    def _install_payload_tokens_for_command(self, command: str) -> list[str]:
        if "|" in command:
            return []
        segments = _split_command_segments(command)
        if len(segments) > 1:
            packages: list[str] = []
            for segment in segments:
                for package in self._install_payload_tokens_for_command(segment):
                    if package not in packages:
                        packages.append(package)
            return packages
        try:
            tokens = shlex.split(command)
        except ValueError:
            return []
        return self._install_payload_tokens_for_package_manager(
            tokens, (self.ctx.facts or SetupFacts()).package_manager
        )

    def _app_group_package_query_candidates(self, group: CommandGroup) -> list[str]:
        candidates: list[str] = []
        planned_packages = self._app_group_planned_packages(group)
        for package in planned_packages:
            if package and package not in candidates:
                candidates.append(package)
        if self.ctx.plan is None:
            return candidates
        group_packages = {package.lower() for package in planned_packages}
        title = group.title.lower()
        for app in self.ctx.plan.apps:
            variants = self._package_name_variants(app.name, app.package_name)
            if not variants:
                continue
            if (
                variants.intersection(group_packages)
                or app.name.lower() in title
                or (app.package_name or "").lower() in group_packages
            ):
                for variant in sorted(variants):
                    if variant not in candidates:
                        candidates.append(variant)
        return candidates

    def _planned_package_is_installed(
        self, package: str, installed_packages: set[str], group: CommandGroup
    ) -> bool:
        if package in installed_packages:
            return True
        variants = self._package_name_variants("", package)
        if self.ctx.plan is not None:
            package_lower = package.lower()
            for app in self.ctx.plan.apps:
                app_variants = self._package_name_variants(app.name, app.package_name)
                if (
                    package_lower in app_variants
                    or (app.package_name or "").lower() == package_lower
                ):
                    variants.update(app_variants)
        return bool(variants.intersection(installed_packages))

    def _mark_apps_installed_by_prefilter(
        self,
        installed_in_group: set[str],
        installed_packages: set[str],
        group: CommandGroup,
    ) -> None:
        if self.ctx.plan is None:
            return
        for app in self.ctx.plan.apps:
            if app.status in {
                PlanItemStatus.INSTALLED,
                PlanItemStatus.MANUAL_FOLLOWUP,
                PlanItemStatus.EXPLICITLY_SKIPPED,
            }:
                continue
            variants = self._package_name_variants(app.name, app.package_name)
            app_matches_group = bool(
                variants.intersection(installed_in_group)
                or variants.intersection(installed_packages)
            )
            if not app_matches_group:
                continue
            app.status = PlanItemStatus.INSTALLED
            app.reason = app.reason or "Already installed before this app command group ran."

    def _mark_installed_apps_from_native_package_db(self) -> None:
        """Update app plan rows from the target package DB before running app commands.

        Reruns against an already-configured target often receive a fresh plan whose
        app rows are pending again. A package preflight here lets the normal app
        audit skip the entire stage before service-only follow-up commands, such as
        enabling Docker again, can run unnecessarily.
        """

        if self.ctx.plan is None or self.ctx.facts is None:
            return
        candidates: dict[str, AppPlanItem] = {}
        for app in self.ctx.plan.apps:
            if app.status in {
                PlanItemStatus.INSTALLED,
                PlanItemStatus.COMPLETED,
                PlanItemStatus.EXPLICITLY_SKIPPED,
                PlanItemStatus.MANUAL_FOLLOWUP,
            }:
                continue
            variants = self._package_name_variants(app.name, app.package_name)
            if not variants:
                continue
            for variant in sorted(variants):
                candidates.setdefault(variant, app)
        if not candidates:
            return
        installed = self._query_installed_native_packages(sorted(candidates), self.ctx.facts)
        if not installed:
            return
        marked: list[str] = []
        for app in self.ctx.plan.apps:
            if app.status in {
                PlanItemStatus.INSTALLED,
                PlanItemStatus.COMPLETED,
                PlanItemStatus.EXPLICITLY_SKIPPED,
                PlanItemStatus.MANUAL_FOLLOWUP,
            }:
                continue
            variants = self._package_name_variants(app.name, app.package_name)
            if not variants.intersection(installed):
                continue
            app.status = PlanItemStatus.INSTALLED
            app.reason = app.reason or "Already installed on target before app stage ran."
            marked.append(app.name)
        if not marked:
            return
        stage_state = self.ctx.stage_state.setdefault(SetupStage.APPS.value, {})
        existing = list(stage_state.get("already_installed_apps_from_package_db", []) or [])
        for app_name in sorted(marked):
            if app_name not in existing:
                existing.append(app_name)
        stage_state["already_installed_apps_from_package_db"] = existing
        self.runlog.record(
            "apps_marked_installed_from_package_db",
            stage=SetupStage.APPS.value,
            apps=sorted(marked),
            installed_packages=sorted(installed),
        )

    def _query_installed_native_packages(self, packages: list[str], facts: SetupFacts) -> set[str]:
        package_list = [package for package in dict.fromkeys(packages) if package]
        if not package_list:
            return set()
        checks = {
            "pacman": 'pacman -Qq "$p" >/dev/null 2>&1',
            "apt": (
                "dpkg-query -W -f='${db:Status-Abbrev}\\n' \"$p\" 2>/dev/null "
                "| grep -qx 'ii ' || snap list \"$p\" >/dev/null 2>&1"
            ),
            "dnf": 'rpm -q "$p" >/dev/null 2>&1',
            "zypper": 'rpm -q "$p" >/dev/null 2>&1',
            "apk": 'apk info -e "$p" >/dev/null 2>&1',
            "xbps": 'xbps-query "$p" >/dev/null 2>&1',
            "emerge": 'qlist -IC "$p" >/dev/null 2>&1',
        }
        check = checks.get(facts.package_manager)
        if not check:
            return set()
        quoted_packages = " ".join(shlex.quote(package) for package in package_list)
        command = (
            f"for p in {quoted_packages}; do "
            f"if {check}; then printf 'INSTALLED:%s\\n' \"$p\"; "
            "else printf 'MISSING:%s\\n' \"$p\"; fi; done"
        )
        rc, stdout, stderr = self.runtime.ssh_exec(command, 30)
        self.runlog.record(
            "app_package_precheck",
            stage=SetupStage.APPS.value,
            package_manager=facts.package_manager,
            command=command,
            rc=rc,
            stdout_tail=(stdout or "")[-800:],
            stderr_tail=(stderr or "")[-800:],
        )
        if rc != 0:
            return set()
        installed: set[str] = set()
        for line in (stdout or "").splitlines():
            if line.startswith("INSTALLED:"):
                installed.add(line.split(":", 1)[1].strip())
        return installed

    def _drop_installed_targets_from_app_install_command(
        self, command: str, installed: set[str], facts: SetupFacts
    ) -> str | None:
        if any(token in command for token in ("&&", "||", ";", "|", "\n")):
            return None
        try:
            tokens = shlex.split(command)
        except ValueError:
            return None
        payload = self._install_payload_tokens_for_package_manager(tokens, facts.package_manager)
        if not payload or not any(package in payload for package in installed):
            return None
        filtered = [token for token in tokens if token not in installed]
        filtered_payload = self._install_payload_tokens_for_package_manager(
            filtered, facts.package_manager
        )
        if not filtered_payload:
            return ""
        return shlex.join(self._add_no_reinstall_flag(filtered, facts.package_manager))

    @staticmethod
    def _install_payload_tokens_for_package_manager(
        tokens: list[str], package_manager: str
    ) -> list[str]:
        def queryable_payload(items: list[str]) -> list[str]:
            return [
                item
                for item in items
                if not item.startswith("-")
                and SetupOrchestrator._is_queryable_package_payload_token(item)
            ]

        tokens = SetupOrchestrator._strip_command_wrappers(tokens)
        if package_manager == "pacman" or any(tool in tokens for tool in ("pacman", "yay", "paru")):
            for idx, token in enumerate(tokens):
                if token == "-S" or token.startswith("-S"):
                    return queryable_payload(tokens[idx + 1 :])
        if package_manager == "apt" or any(tool in tokens for tool in ("apt", "apt-get")):
            for idx, token in enumerate(tokens):
                if token == "install":
                    return queryable_payload(tokens[idx + 1 :])
        if package_manager in {"dnf", "zypper"} or any(
            tool in tokens for tool in ("dnf", "zypper")
        ):
            for idx, token in enumerate(tokens):
                if token in {"install", "in"}:
                    return queryable_payload(tokens[idx + 1 :])
        if package_manager == "apk" or "apk" in tokens:
            for idx, token in enumerate(tokens):
                if token == "add":
                    return queryable_payload(tokens[idx + 1 :])
        if package_manager in {"xbps", "emerge"} or any(
            tool in tokens for tool in ("xbps-install", "emerge")
        ):
            tool_names = {"xbps-install", "emerge"}
            for idx, token in enumerate(tokens):
                if token in tool_names:
                    return queryable_payload(tokens[idx + 1 :])
        return []

    @staticmethod
    def _is_queryable_package_payload_token(token: str) -> bool:
        """Return whether a package-manager payload token is a package DB name."""

        value = (token or "").strip()
        lowered = value.lower()
        if not value:
            return False
        if "://" in lowered or "/" in value or "\\" in value:
            return False
        if "$(" in value or "${" in value:
            return False
        if lowered.endswith(
            (
                ".apk",
                ".deb",
                ".pkg.tar",
                ".pkg.tar.xz",
                ".pkg.tar.zst",
                ".rpm",
            )
        ):
            return False
        return True

    @staticmethod
    def _strip_command_wrappers(tokens: list[str]) -> list[str]:
        stripped = list(tokens)
        while stripped:
            token = stripped[0]
            if token == "sudo":
                stripped = stripped[1:]
                while stripped and stripped[0] in {"-E", "-H", "-n", "-S"}:
                    stripped = stripped[1:]
                continue
            if token == "env":
                stripped = stripped[1:]
                continue
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", token):
                stripped = stripped[1:]
                continue
            break
        return stripped

    @staticmethod
    def _add_no_reinstall_flag(tokens: list[str], package_manager: str) -> list[str]:
        """Prefer package-manager flags that skip already-satisfied targets.

        This is deliberately generic: it applies to supported package managers by
        command shape, not to app names. It reduces wasted repair work if a model
        still broadens an install command after QuickHatch has package facts.
        """

        updated = list(tokens)
        stripped = SetupOrchestrator._strip_command_wrappers(updated)
        prefix_len = len(updated) - len(stripped)

        if package_manager == "pacman" or any(
            tool in stripped for tool in ("pacman", "yay", "paru")
        ):
            for idx, token in enumerate(stripped):
                if token == "-S" or token.startswith("-S"):
                    if "--needed" not in updated:
                        updated.insert(prefix_len + idx + 1, "--needed")
                    break
        return updated

    def _mark_apps_manual_followup_by_names(self, names: list[str], reason: str) -> None:
        if self.ctx.plan is None:
            return
        wanted = {name.lower() for name in names if name}
        for app in self.ctx.plan.apps:
            if app.name.lower() in wanted:
                app.status = PlanItemStatus.MANUAL_FOLLOWUP
                app.reason = reason

    def _normalize_package_verification_command(
        self, command: str, *, stage: SetupStage, group: CommandGroup | None = None
    ) -> str:
        facts = self.ctx.facts or SetupFacts()
        if (
            stage == SetupStage.APPS
            and group is not None
            and group.packages
            and facts.package_manager in {"pacman", "apt", "dnf", "zypper", "apk", "xbps", "emerge"}
            and self._is_simple_app_launcher_probe(command)
        ):
            return self._package_verification_command(group.packages, facts)
        if facts.package_manager != "pacman" or stage not in _INSTALLED_NONROOT_PRIVILEGED_STAGES:
            return command
        match = _PACMAN_QI_SINGLE_RE.match((command or "").strip())
        if not match:
            return command
        name = shlex.quote(match.group("name"))
        return f"(pacman -Qi {name} >/dev/null 2>&1 || pacman -Qg {name} >/dev/null 2>&1)"

    @staticmethod
    def _is_simple_app_launcher_probe(command: str) -> bool:
        stripped = (command or "").strip()
        if not stripped or any(token in stripped for token in ("&&", "||", ";", "|", ">", "<")):
            return False
        return bool(_SIMPLE_APP_LAUNCHER_PROBE_RE.match(stripped))

    def _soft_fail_desktop_cosmetic_verification(
        self,
        *,
        stage: SetupStage,
        group: CommandGroup,
        verification: str,
        rc: int,
        stdout: str,
        stderr: str,
        stage_state: dict[str, Any],
    ) -> bool:
        if stage != SetupStage.DESKTOP:
            return False
        if not self._is_cosmetic_desktop_verification(verification):
            return False
        if not (group.optional or group.optional_slow):
            return False
        reason = (
            f"Cosmetic desktop verification did not pass for '{group.title}' "
            f"({verification}). Leaving this theme/icon/font check as manual follow-up."
        )
        stage_state.setdefault("manual_followups", []).append(
            {
                "group_id": group.id,
                "reason": reason,
                "verification": verification,
            }
        )
        self.runlog.record(
            "desktop_cosmetic_verification_manual_followup",
            stage=stage.value,
            group_id=group.id,
            command=verification,
            rc=rc,
            stdout_tail=(stdout or "")[-500:],
            stderr_tail=(stderr or "")[-500:],
            reason=reason,
        )
        self.bridge.add_message("agent", f"[harness] {reason}")
        return True

    @staticmethod
    def _is_cosmetic_desktop_verification(command: str) -> bool:
        lowered = (command or "").lower()
        if any(
            core in lowered
            for core in (
                "plasma-desktop",
                "plasma-meta",
                "plasma-workspace",
                "kde-applications",
                "sddm",
                "gdm",
                "lightdm",
                "xorg",
                "wayland",
                "xdg-desktop-portal",
                "networkmanager",
                "pipewire",
                "dolphin",
                "konsole",
            )
        ):
            return False
        has_cosmetic_hint = any(
            hint in lowered
            for hint in (
                "look-and-feel",
                "lookandfeel",
                "theme",
                "icon",
                "cursor",
                "wallpaper",
                "font",
                "ttf-",
                "otf-",
                "noto",
                "inter",
                "kvantum",
                "catppuccin",
                "sweet",
                "candy",
                "papirus",
                "bibata",
            )
        )
        if not has_cosmetic_hint:
            return False
        if re.search(r"\b(?:pacman|dpkg-query|rpm|apk|xbps-query|qlist)\b", lowered):
            return True
        return any(
            path in lowered
            for path in (
                "/usr/share/plasma/look-and-feel",
                "/usr/share/icons",
                "/usr/share/themes",
                "/usr/share/wallpapers",
                "/usr/share/fonts",
                "/usr/local/share/fonts",
                "/.local/share/icons",
                "/.local/share/themes",
                "/.local/share/wallpapers",
                "/.config/gtk-",
                "kdeglobals",
                "kwinrc",
            )
        )

    def _auto_repair_missing_repo_targets(
        self,
        stage: SetupStage,
        group: CommandGroup,
        failure: GroupFailure,
    ) -> CommandGroup | None:
        if failure.failure_type != "command":
            return None
        missing = self._missing_repo_targets_from_output(failure.stdout, failure.stderr)
        if not missing:
            return None
        if stage == SetupStage.APPS:
            return self._auto_repair_missing_app_targets(group, failure, missing)
        repaired_commands: list[str] = []
        changed = False
        for command in group.commands:
            if command == failure.command:
                repaired = self._drop_missing_targets_from_install_command(command, missing)
                if repaired and repaired != command:
                    repaired_commands.append(repaired)
                    changed = True
                    continue
            repaired_commands.append(command)
        if not changed:
            return None
        repaired_group = copy.deepcopy(group)
        repaired_group.id = f"{group.id}-without-missing-targets"
        repaired_group.title = f"{group.title} (without unavailable repo targets)"
        repaired_group.commands = repaired_commands
        repaired_group.packages = [pkg for pkg in group.packages if pkg not in missing]
        repaired_group.rationale = (
            f"{group.rationale}\nQuickHatch removed unavailable repository target(s) "
            f"{', '.join(sorted(missing))} from this non-app stage so remaining "
            "validated packages can continue."
        ).strip()
        self.bridge.add_message(
            "agent",
            "[harness] Removed unavailable non-app repo target(s) "
            f"{', '.join(sorted(missing))} and will retry the group.",
        )
        self.runlog.record(
            "auto_repair_missing_repo_targets",
            stage=stage.value,
            group_id=group.id,
            missing=sorted(missing),
            commands=repaired_commands,
        )
        self.ctx.stage_state.setdefault(stage.value, {}).setdefault("manual_followups", []).append(
            {
                "group_id": group.id,
                "reason": (
                    "Unavailable repository target(s) were dropped from "
                    f"'{group.title}': {', '.join(sorted(missing))}. "
                    "Review or choose alternatives after setup."
                ),
            }
        )
        return repaired_group

    def _auto_repair_missing_app_targets(
        self,
        group: CommandGroup,
        failure: GroupFailure,
        missing: set[str],
        *,
        manual_after_repair: bool = False,
        manual_reason: str = "",
    ) -> CommandGroup | None:
        facts = self.ctx.facts or SetupFacts()
        missing_lower = {item.lower() for item in missing}
        failed_payload = self._install_payload_tokens_for_command(failure.command)
        if not failed_payload:
            return None
        affected_apps: list[str] = []
        if self.ctx.plan is not None:
            for app in self.ctx.plan.apps:
                variants = self._package_name_variants(app.name, app.package_name)
                if not variants.intersection(missing_lower):
                    continue
                affected_apps.append(app.name)

        resolutions, unresolved = self._resolve_missing_app_packages(missing, affected_apps)
        if resolutions:
            for resolution in resolutions:
                self._apply_app_package_resolution(resolution)
        if unresolved and not manual_after_repair:
            self.runlog.record(
                "app_missing_package_resolution_deferred_to_repair",
                stage=SetupStage.APPS.value,
                group_id=group.id,
                missing=sorted(unresolved),
                affected_apps=sorted(affected_apps),
            )
            return None
        if unresolved:
            self._mark_unresolved_missing_app_packages_manual_followup(
                unresolved,
                affected_apps,
                reason=manual_reason
                or (
                    "Package manager reported unavailable app package target(s) after "
                    "safe search/repair attempts: " + ", ".join(sorted(unresolved))
                ),
            )

        repaired_commands: list[str] = []
        changed = False
        for command in group.commands:
            if command == failure.command:
                repaired = self._drop_missing_targets_from_install_command(
                    command,
                    missing,
                    package_manager=facts.package_manager,
                )
                if repaired is not None and repaired != command:
                    if repaired:
                        repaired_commands.append(repaired)
                    changed = True
                    continue
            repaired_commands.append(command)
        for resolution in resolutions:
            if resolution.command and resolution.command not in repaired_commands:
                repaired_commands.append(resolution.command)
                changed = True
        if not changed:
            return None

        remaining_packages = [pkg for pkg in group.packages if pkg.lower() not in missing_lower]
        remaining_packages.extend(
            resolution.package_name
            for resolution in resolutions
            if resolution.package_name not in remaining_packages
        )
        repaired_group = copy.deepcopy(group)
        repaired_group.id = f"{group.id}-without-missing-app-targets"
        repaired_group.title = f"{group.title} (without unavailable app packages)"
        repaired_group.commands = repaired_commands
        repaired_group.packages = remaining_packages
        verification = [
            resolution.verification for resolution in resolutions if resolution.verification
        ]
        native_packages = [
            package
            for package in remaining_packages
            if package
            and not package.lower().startswith(("org.", "com.", "io.", "net."))
            and package
            not in {
                resolution.package_name
                for resolution in resolutions
                if resolution.source == "flatpak"
            }
        ]
        if native_packages:
            verification.insert(0, self._package_verification_command(native_packages, facts))
        repaired_group.verification = verification
        repaired_group.rationale = (
            f"{group.rationale}\nQuickHatch treated catalog/planner package names as hints. "
            f"Unavailable app package target(s) {', '.join(sorted(missing))} were removed "
            "from the failed command; resolved replacements were added when target search "
            "found safe matches, and unresolved app(s) were left for manual follow-up only "
            "after repair attempts."
        ).strip()
        if unresolved:
            self.bridge.add_message(
                "agent",
                "[harness] App package target(s) unavailable after repair/search; leaving "
                + ", ".join(sorted(unresolved))
                + " for manual follow-up and retrying remaining app packages.",
            )
        if resolutions:
            self.bridge.add_message(
                "agent",
                "[harness] Resolved stale app package hint(s) from target search: "
                + ", ".join(
                    f"{item.original_package} -> {item.package_name} ({item.source})"
                    for item in resolutions
                ),
            )
        self.runlog.record(
            "auto_repair_missing_app_targets",
            stage=SetupStage.APPS.value,
            group_id=group.id,
            missing=sorted(missing),
            affected_apps=sorted(affected_apps),
            resolved=[asdict(resolution) for resolution in resolutions],
            unresolved=sorted(unresolved),
            commands=repaired_commands,
        )
        return repaired_group

    def _resolve_missing_app_packages(
        self,
        missing: set[str],
        affected_apps: list[str],
    ) -> tuple[list[AppPackageResolution], set[str]]:
        resolutions: list[AppPackageResolution] = []
        unresolved: set[str] = set()
        for target in sorted(missing):
            app = self._app_for_missing_package(target, affected_apps)
            resolution = self._resolve_single_missing_app_package(target, app)
            if resolution is None:
                unresolved.add(target)
                continue
            resolutions.append(resolution)
        return resolutions, unresolved

    def _app_for_missing_package(
        self,
        missing_package: str,
        affected_apps: list[str],
    ) -> AppPlanItem | None:
        if self.ctx.plan is None:
            return None
        missing_lower = missing_package.lower()
        affected = {name.lower() for name in affected_apps}
        for app in self.ctx.plan.apps:
            variants = self._package_name_variants(app.name, app.package_name)
            if missing_lower in variants or variants.intersection({missing_lower}):
                return app
        for app in self.ctx.plan.apps:
            if app.name.lower() in affected:
                return app
        return None

    def _resolve_single_missing_app_package(
        self,
        missing_package: str,
        app: AppPlanItem | None,
    ) -> AppPackageResolution | None:
        facts = self.ctx.facts or SetupFacts()
        app_name = app.name if app else missing_package
        queries = self._app_resolution_queries(app_name, missing_package)
        strategies = self._app_resolution_strategies(facts)
        for source in strategies:
            for query in queries:
                result = self._run_app_resolution_search(source, query, app_name, missing_package)
                if not result:
                    continue
                candidate = self._candidate_from_app_search_result(
                    result,
                    source=source,
                    app_name=app_name,
                    missing_package=missing_package,
                )
                if not candidate or candidate.lower() == missing_package.lower():
                    continue
                command = self._install_command_for_resolved_app(candidate, source, facts)
                verification = self._verification_for_resolved_app(candidate, source, facts)
                if not command or not verification:
                    continue
                return AppPackageResolution(
                    app_name=app_name,
                    original_package=missing_package,
                    package_name=candidate,
                    source=source,
                    command=command,
                    verification=verification,
                    evidence_summary=f"{source} search for {query!r} returned {candidate!r}",
                )
        if self.runtime.native_search_active:
            for query in queries[:1]:
                try:
                    result = self.runtime.web_search(
                        f"{app_name} Linux package {facts.distro_family or facts.package_manager}",
                        5,
                    )
                except Exception as exc:  # pragma: no cover - defensive logging only
                    result = f"[web_search error] {exc}"
                self.runlog.record(
                    "app_missing_package_web_search",
                    stage=SetupStage.APPS.value,
                    app=app_name,
                    missing=missing_package,
                    query=query,
                    result_tail=(result or "")[-1000:],
                    reason=(
                        "Provider/native web search is available; result is recorded for "
                        "the model repair loop, not executed blindly."
                    ),
                )
                break
        return None

    @staticmethod
    def _app_resolution_queries(app_name: str, missing_package: str) -> list[str]:
        queries: list[str] = []
        for value in (missing_package, app_name):
            normalized = str(value or "").strip()
            if normalized and normalized not in queries:
                queries.append(normalized)
        return queries

    @staticmethod
    def _app_resolution_strategies(facts: SetupFacts) -> list[str]:
        if facts.package_manager == "pacman":
            return ["repo", "community"]
        if facts.package_manager in {"apt", "dnf"}:
            return ["repo", "flatpak"]
        if facts.package_manager in {"zypper", "apk", "xbps", "emerge"}:
            return ["repo"]
        return []

    def _run_app_resolution_search(
        self,
        source: str,
        query: str,
        app_name: str,
        missing_package: str,
    ) -> str:
        try:
            if source == "repo":
                result = self.runtime.repo_search(query)
            elif source == "community":
                result = self.runtime.community_search(query)
            elif source == "flatpak":
                result = self._flatpak_search(query)
            else:
                return ""
        except Exception as exc:
            result = f"[{source}_search error] {exc}"
        self.runlog.record(
            "app_missing_package_search",
            stage=SetupStage.APPS.value,
            source=source,
            app=app_name,
            missing=missing_package,
            query=query,
            result_tail=(result or "")[-1000:],
        )
        return result or ""

    def _flatpak_search(self, query: str) -> str:
        query_q = shlex.quote(query)
        command = (
            "if command -v flatpak >/dev/null 2>&1; then "
            f"flatpak search --columns=application,name {query_q} 2>/dev/null | sed -n '1,25p'; "
            "fi"
        )
        rc, stdout, stderr = self.runtime.ssh_exec(command, 45)
        self.runlog.record(
            "app_flatpak_search",
            stage=SetupStage.APPS.value,
            command=command,
            rc=rc,
            stdout_tail=(stdout or "")[-800:],
            stderr_tail=(stderr or "")[-800:],
        )
        if rc != 0:
            return ""
        return stdout or ""

    def _candidate_from_app_search_result(
        self,
        result: str,
        *,
        source: str,
        app_name: str,
        missing_package: str,
    ) -> str:
        for line in (result or "").splitlines():
            candidate = self._package_candidate_from_search_line(line, source)
            if not candidate:
                continue
            if self._candidate_matches_missing_app(
                candidate,
                line=line,
                source=source,
                app_name=app_name,
                missing_package=missing_package,
            ):
                return candidate
        return ""

    @staticmethod
    def _package_candidate_from_search_line(line: str, source: str) -> str:
        stripped = (line or "").strip()
        if not stripped or stripped.startswith("["):
            return ""
        if source == "flatpak":
            token = stripped.split(maxsplit=1)[0]
            return token if "." in token and re.fullmatch(r"[A-Za-z0-9_.-]+", token) else ""
        if "/" in stripped.split(maxsplit=1)[0]:
            token = stripped.split(maxsplit=1)[0].split("/", 1)[1]
            return token if re.fullmatch(r"[A-Za-z0-9@._+:-]+", token) else ""
        if " - " in stripped:
            token = stripped.split(" - ", 1)[0].strip()
            return token if re.fullmatch(r"[A-Za-z0-9@._+:-]+", token) else ""
        if " : " in stripped:
            token = stripped.split(" : ", 1)[0].strip()
            token = re.sub(r"\.(?:x86_64|aarch64|noarch|i686)$", "", token)
            return token if re.fullmatch(r"[A-Za-z0-9@._+:-]+", token) else ""
        token = stripped.split(maxsplit=1)[0]
        return token if re.fullmatch(r"[A-Za-z0-9@._+:-]+", token) else ""

    def _candidate_matches_missing_app(
        self,
        candidate: str,
        *,
        line: str,
        source: str,
        app_name: str,
        missing_package: str,
    ) -> bool:
        variants = self._package_name_variants(app_name, missing_package)
        variants.update(self._package_name_variants("", missing_package))
        candidate_lower = candidate.lower()
        compact_candidate = self._compact_package_token(candidate_lower)
        compact_variants = {self._compact_package_token(variant) for variant in variants}
        if candidate_lower in variants or compact_candidate in compact_variants:
            return True
        if source == "flatpak":
            name_tokens = self._meaningful_app_name_tokens(app_name)
            line_tokens = self._meaningful_app_name_tokens(line)
            return bool(name_tokens and name_tokens <= line_tokens)
        return False

    @staticmethod
    def _compact_package_token(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

    @staticmethod
    def _meaningful_app_name_tokens(value: str) -> set[str]:
        tokens = {
            token for token in re.split(r"[^a-z0-9]+", str(value or "").lower()) if len(token) >= 3
        }
        return tokens - {"app", "desktop", "reader", "browser", "linux", "client"}

    def _install_command_for_resolved_app(
        self,
        package_name: str,
        source: str,
        facts: SetupFacts,
    ) -> str:
        if source == "community":
            return self._community_install_command([package_name], facts)
        if source == "flatpak":
            return self._flatpak_install_command([package_name])
        return build_install_command([package_name], "repo", facts)

    def _verification_for_resolved_app(
        self,
        package_name: str,
        source: str,
        facts: SetupFacts,
    ) -> str:
        if source == "flatpak":
            return self._flatpak_verification_command([package_name])
        return self._package_verification_command([package_name], facts)

    def _apply_app_package_resolution(self, resolution: AppPackageResolution) -> None:
        if self.ctx.plan is None:
            return
        for app in self.ctx.plan.apps:
            if app.name != resolution.app_name:
                continue
            app.package_name = resolution.package_name
            app.source = resolution.source
            app.status = PlanItemStatus.PENDING
            app.reason = ""
            break

    def _mark_unresolved_missing_app_packages_manual_followup(
        self,
        unresolved: set[str],
        affected_apps: list[str],
        *,
        reason: str,
    ) -> None:
        stage_state = self.ctx.stage_state.setdefault(SetupStage.APPS.value, {})
        affected_set = {name.lower() for name in affected_apps}
        unresolved_lower = {item.lower() for item in unresolved}
        marked: list[str] = []
        if self.ctx.plan is not None:
            for app in self.ctx.plan.apps:
                variants = self._package_name_variants(app.name, app.package_name)
                if (
                    not variants.intersection(unresolved_lower)
                    and app.name.lower() not in affected_set
                ):
                    continue
                app.status = PlanItemStatus.MANUAL_FOLLOWUP
                app.reason = reason
                marked.append(app.name)
        stage_state.setdefault("manual_followups", []).append(
            {
                "apps": sorted(marked or affected_apps),
                "packages": sorted(unresolved),
                "reason": reason,
            }
        )

    def _auto_repair_arch_pacman_fetch_failure(
        self,
        stage: SetupStage,
        group: CommandGroup,
        failure: GroupFailure,
        stage_state: dict[str, Any],
    ) -> CommandGroup | None:
        facts = self.ctx.facts or SetupFacts()
        if facts.package_manager != "pacman" or failure.failure_type != "command":
            return None
        text = "\n".join([failure.stdout or "", failure.stderr or "", failure.detail or ""]).lower()
        if "failed retrieving file" not in text or "404" not in text:
            return None
        repaired_command = self._arch_pacman_refresh_retry_install_command(failure.command)
        if not repaired_command:
            return None
        key = f"{group.id}:{failure.command}"
        attempted = set(stage_state.get("arch_pacman_fetch_repairs") or [])
        if key in attempted:
            return None
        attempted.add(key)
        stage_state["arch_pacman_fetch_repairs"] = sorted(attempted)

        repaired_group = copy.deepcopy(group)
        repaired_group.id = f"{group.id}-after-pacman-db-refresh"
        repaired_group.title = f"{group.title} (after pacman database refresh)"
        repaired_group.commands = [
            repaired_command if command == failure.command else command
            for command in group.commands
        ]
        repaired_group.rationale = (
            f"{group.rationale}\nQuickHatch retried this Arch package install with "
            "`pacman -Syyu` because the target's package database/mirror state produced "
            "download 404s."
        ).strip()
        self.bridge.add_message(
            "agent",
            "[harness] Pacman package download returned mirror 404s; retrying with "
            "a full package database refresh.",
        )
        self.runlog.record(
            "auto_repair_arch_pacman_fetch_failure",
            stage=stage.value,
            group_id=group.id,
            original_command=failure.command,
            repaired_command=repaired_command,
        )
        return repaired_group

    def _auto_repair_arch_aur_pgp_key_failure(
        self,
        stage: SetupStage,
        group: CommandGroup,
        failure: GroupFailure,
        stage_state: dict[str, Any],
    ) -> CommandGroup | None:
        facts = self.ctx.facts or SetupFacts()
        if (
            stage != SetupStage.APPS
            or facts.package_manager != "pacman"
            or failure.failure_type != "command"
        ):
            return None
        try:
            tokens = shlex.split(failure.command)
        except ValueError:
            return None
        if not any(token.rsplit("/", 1)[-1] in {"yay", "paru"} for token in tokens):
            return None
        text = "\n".join([failure.stdout or "", failure.stderr or "", failure.detail or ""])
        lowered = text.lower()
        if "pgp keys need importing" not in lowered and "problem importing keys" not in lowered:
            return None
        blocked_packages = self._arch_aur_pgp_required_packages(text)
        if not blocked_packages:
            return None
        repaired_command = self._drop_missing_targets_from_install_command(
            failure.command,
            blocked_packages,
            package_manager=facts.package_manager,
        )
        if not repaired_command or repaired_command == failure.command:
            return None
        key = f"{group.id}:{failure.command}:{','.join(sorted(blocked_packages))}"
        attempted = set(stage_state.get("arch_aur_pgp_key_repairs") or [])
        if key in attempted:
            return None
        attempted.add(key)
        stage_state["arch_aur_pgp_key_repairs"] = sorted(attempted)

        affected_apps = self._app_names_for_package_targets(blocked_packages)
        reason = (
            "AUR package PGP key import failed for "
            f"{', '.join(sorted(blocked_packages))}; retrying remaining app packages and "
            "leaving the affected app(s) for manual follow-up."
        )
        self._mark_unresolved_missing_app_packages_manual_followup(
            blocked_packages,
            affected_apps,
            reason=reason,
        )
        remaining_packages = [
            package for package in group.packages if package.lower() not in blocked_packages
        ]
        repaired_group = copy.deepcopy(group)
        repaired_group.id = f"{group.id}-without-aur-pgp-key-failures"
        repaired_group.title = f"{group.title} (without AUR PGP key failures)"
        repaired_group.commands = [
            repaired_command if command == failure.command else command
            for command in group.commands
        ]
        repaired_group.packages = remaining_packages
        repaired_group.verification = (
            [self._package_verification_command(remaining_packages, facts)]
            if remaining_packages
            else []
        )
        repaired_group.rationale = (
            f"{group.rationale}\nQuickHatch split an AUR app install after a package-specific "
            "PGP key import failure so other requested apps could continue."
        ).strip()
        self.bridge.add_message(
            "agent",
            "[harness] AUR PGP key import failed for "
            + ", ".join(sorted(blocked_packages))
            + "; retrying remaining app packages and leaving the affected app(s) for manual follow-up.",
        )
        self.runlog.record(
            "auto_repair_arch_aur_pgp_key_failure",
            stage=stage.value,
            group_id=group.id,
            blocked_packages=sorted(blocked_packages),
            affected_apps=sorted(affected_apps),
            original_command=failure.command,
            repaired_command=repaired_command,
        )
        return repaired_group

    @staticmethod
    def _arch_aur_pgp_required_packages(text: str) -> set[str]:
        packages: set[str] = set()
        for match in re.findall(
            r"required by:\s*([^\r\n]+)",
            text or "",
            flags=re.IGNORECASE,
        ):
            for token in re.split(r"[\s,]+", match.strip()):
                if re.search(r"[A-Za-z0-9]", token or "") and re.fullmatch(
                    r"[A-Za-z0-9@._+:-]+", token or ""
                ):
                    packages.add(token.lower())
        return packages

    def _app_names_for_package_targets(self, packages: set[str]) -> list[str]:
        if self.ctx.plan is None:
            return []
        targets = {package.lower() for package in packages}
        names: list[str] = []
        for app in self.ctx.plan.apps:
            if self._package_name_variants(app.name, app.package_name).intersection(targets):
                names.append(app.name)
        return names

    @staticmethod
    def _arch_pacman_refresh_retry_install_command(command: str) -> str | None:
        if any(token in command for token in ("&&", "||", ";", "|", "\n")):
            return None
        try:
            tokens = shlex.split(command)
        except ValueError:
            return None
        if not tokens:
            return None
        prefix: list[str] = []
        tool_idx = 0
        if tokens[0] == "sudo":
            prefix = ["sudo"]
            tool_idx = 1
        if tool_idx >= len(tokens) or tokens[tool_idx].rsplit("/", 1)[-1] != "pacman":
            return None
        op_idx = next(
            (
                idx
                for idx, token in enumerate(tokens[tool_idx + 1 :], start=tool_idx + 1)
                if token.startswith("-") and not token.startswith("--") and "S" in token
            ),
            -1,
        )
        if op_idx < 0:
            return None
        tail = tokens[op_idx + 1 :]
        payload = [token for token in tail if not token.startswith("-")]
        if not payload:
            return None
        options: list[str] = []
        for token in [*tokens[tool_idx + 1 : op_idx], *tail]:
            if not token.startswith("-") or (token.startswith("-") and not token.startswith("--")):
                continue
            if token not in options:
                options.append(token)
        for required in ("--needed", "--noconfirm"):
            if required not in options:
                options.append(required)
        return shlex.join([*prefix, "pacman", "-Syyu", *options, *payload])

    @staticmethod
    def _missing_repo_targets_from_output(*texts: str) -> set[str]:
        text = "\n".join(str(item or "") for item in texts)
        missing = set(re.findall(r"target not found:\s*([A-Za-z0-9@._+:-]+)", text))
        missing.update(re.findall(r"Unable to locate package\s+([A-Za-z0-9@._+:-]+)", text))
        missing.update(re.findall(r"No match for argument:\s*([A-Za-z0-9@._+:-]+)", text))
        for match in re.findall(r"Unable to find a match:\s*([A-Za-z0-9@._+:\t -]+)", text):
            missing.update(
                token
                for token in re.split(r"\s+", match.strip())
                if re.fullmatch(r"[A-Za-z0-9@._+:-]+", token or "")
            )
        return missing

    @staticmethod
    def _drop_missing_targets_from_install_command(
        command: str,
        missing: set[str],
        *,
        package_manager: str = "",
    ) -> str | None:
        if any(token in command for token in ("&&", "||", ";", "|", "\n")):
            return None
        try:
            tokens = shlex.split(command)
        except ValueError:
            return None
        if not tokens:
            return None
        payload = SetupOrchestrator._install_payload_tokens_for_package_manager(
            tokens,
            package_manager,
        )
        if not payload and any(tool in tokens for tool in ("pacman", "pacstrap")):
            payload = SetupOrchestrator._install_payload_tokens(tokens)
        if not payload:
            return None
        missing_lower = {item.lower() for item in missing}
        filtered: list[str] = []
        removed = False
        for token in tokens:
            if token in payload and token.lower() in missing_lower:
                removed = True
                continue
            filtered.append(token)
        if not removed:
            return None
        if filtered == tokens:
            return None
        remaining_payload = SetupOrchestrator._install_payload_tokens_for_package_manager(
            filtered,
            package_manager,
        )
        if not remaining_payload and any(tool in filtered for tool in ("pacman", "pacstrap")):
            remaining_payload = SetupOrchestrator._install_payload_tokens(filtered)
        if not remaining_payload:
            return ""
        return shlex.join(filtered)

    @staticmethod
    def _install_payload_tokens(tokens: list[str]) -> list[str]:
        if "pacman" in tokens:
            for idx, token in enumerate(tokens):
                if token == "-S" or token.startswith("-S"):
                    return [item for item in tokens[idx + 1 :] if not item.startswith("-")]
        if "pacstrap" in tokens:
            mount_idx = next(
                (idx for idx, token in enumerate(tokens) if token in {"/mnt", "/target"}),
                -1,
            )
            if mount_idx >= 0:
                return [
                    item
                    for item in tokens[mount_idx + 1 :]
                    if not item.startswith("-") and not item.startswith("$")
                ]
        return []

    def _ssh_exec_with_transient_retries(
        self,
        command: str,
        timeout: int,
        *,
        stage: SetupStage,
        group_id: str,
        label: str,
    ) -> tuple[int, str, str]:
        max_attempts = 5
        last_result = (1, "", "command did not run")
        for attempt in range(1, max_attempts + 1):
            rc, stdout, stderr = self.runtime.ssh_exec(command, timeout)
            last_result = (rc, stdout, stderr)
            if rc == 0:
                return last_result
            if not self._looks_like_transient_ssh_disconnect(stdout, stderr):
                return last_result
            if attempt == max_attempts:
                return last_result
            delay = min(2 * attempt, 10)
            self.bridge.add_message(
                "agent",
                "Transient SSH transport failure during "
                f"{label}; retrying the same command "
                f"({attempt + 1}/{max_attempts}) after {delay}s…",
            )
            self.runlog.record(
                "ssh_transport_retry",
                stage=stage.value,
                group_id=group_id,
                label=label,
                command=command,
                attempt=attempt,
                rc=rc,
                stdout_tail=(stdout or "")[-500:],
                stderr_tail=(stderr or "")[-500:],
            )
            time.sleep(delay)
        return last_result

    def _commands_for_group(self, stage: SetupStage, group: CommandGroup) -> list[str]:
        commands = self._drop_live_install_harness_owned_commands(stage, group)
        commands = self._combine_standalone_assignments(commands)
        commands = [self._normalize_fedora_kickstart_command(command) for command in commands]
        commands = [
            self._normalize_fedora_dnf_config_manager_command(command) for command in commands
        ]
        commands = [
            self._normalize_fedora_virtualbox_service_command(command, stage=stage, group=group)
            for command in commands
        ]
        commands = [
            self._normalize_fedora_removed_package_command(command, stage=stage, group=group)
            for command in commands
        ]
        commands = [
            self._normalize_root_package_manager_run_as_target_user(
                command, stage=stage, group=group
            )
            for command in commands
        ]
        commands = [self._normalize_mountpoint_probe_command(command) for command in commands]
        commands = self._inject_debian_live_debootstrap_prereqs(stage, group, commands)
        commands = self._prefer_debian_live_cdrom_debootstrap_mirror(stage, group, commands)
        commands = self._inject_debian_live_chroot_bind_mounts(stage, group, commands)
        commands = self._normalize_live_resolv_conf_copies(stage, group, commands)
        commands = self._inject_fedora_live_shadow_lock_cleanup(stage, group, commands)
        commands = self._normalize_debian_live_efi_layout(stage, group, commands)
        commands = self._inject_arch_live_vconsole_before_pacstrap(stage, group, commands)
        commands = self._inject_arch_live_keyring_before_pacstrap(stage, group, commands)
        commands = self._inject_target_home_writability_prep(stage, group, commands)
        commands = self._inject_apt_external_repo_prereqs(stage, group, commands)
        commands = self._inject_ubuntu_apt_component_prereqs(stage, group, commands)
        commands = self._normalize_apt_network_timeouts(stage, group, commands)
        commands = self._normalize_apt_noninteractive(stage, group, commands)
        commands = [
            self._normalize_live_blkid_fstab_command(command, stage=stage) for command in commands
        ]
        commands = [
            self._normalize_live_nonroot_chroot_command(command, stage=stage)
            for command in commands
        ]
        commands = self._drop_model_arch_aur_helper_bootstrap_commands(stage, group, commands)
        if self._is_arch_nvidia_driver_group(stage, group, commands):
            commands = self._normalize_arch_nvidia_commands(commands)
        if self._needs_arch_aur_helper_bootstrap(commands):
            bootstrap_command = self._arch_yay_bootstrap_command()
            commands = [bootstrap_command, *commands]
            self.runlog.record(
                "arch_aur_helper_bootstrap_injected",
                stage=stage.value,
                group_id=group.id,
                command=bootstrap_command,
            )
        if not self._needs_live_disk_prep(stage, commands):
            return [
                self._normalize_installed_nonroot_privileged_command(command, stage=stage)
                for command in commands
            ]

        first_destructive = self._first_live_disk_destructive_index(commands)
        commands_before_destructive = [
            command.strip().lower() for command in commands[:first_destructive]
        ]
        has_unmount = any(
            "umount" in command and "/mnt" in command for command in commands_before_destructive
        )
        has_swapoff = any("swapoff" in command for command in commands_before_destructive)
        if has_unmount and has_swapoff:
            return commands

        prep_commands = self._live_disk_prep_commands(commands)
        patched_commands = prep_commands + commands
        self.runlog.record(
            "live_disk_prep_injected",
            stage=stage.value,
            group_id=group.id,
            commands=prep_commands,
        )
        patched_commands = self._combine_standalone_assignments(patched_commands)
        return [
            self._normalize_installed_nonroot_privileged_command(command, stage=stage)
            for command in patched_commands
        ]

    def _normalize_live_blkid_fstab_command(self, command: str, *, stage: SetupStage) -> str:
        facts = self.ctx.facts or SetupFacts()
        if stage != SetupStage.INSTALL_OR_SKIP or not facts.is_live:
            return command
        if "blkid" not in command or "/etc/fstab" not in command:
            return command
        rewritten = re.sub(r"\$\((?!sudo\s)(blkid\s+)", r"$(sudo \1", command)
        if rewritten != command:
            self.runlog.record(
                "live_blkid_fstab_normalized",
                stage=stage.value,
                original=command,
                rewritten=rewritten,
                reason="live installs may need sudo to read block UUIDs for fstab",
            )
        return rewritten

    def _normalize_live_nonroot_chroot_command(self, command: str, *, stage: SetupStage) -> str:
        facts = self.ctx.facts or SetupFacts()
        if stage != SetupStage.INSTALL_OR_SKIP or not facts.is_live or facts.is_root:
            return command
        rewritten = _LIVE_NONROOT_CHROOT_RE.sub(
            lambda match: (
                f"{match.group('prefix')}sudo "
                f"{self._live_chroot_tool_for_family(match.group('tool'), facts)} "
                f"{match.group('target')}"
            ),
            command,
        )
        if rewritten != command:
            self.runlog.record(
                "live_nonroot_chroot_normalized",
                stage=stage.value,
                original=command,
                rewritten=rewritten,
                reason="live non-root sessions need sudo for chroot verification/commands",
            )
        return rewritten

    def _normalize_live_nonroot_target_path_verification(
        self, command: str, *, stage: SetupStage
    ) -> str:
        facts = self.ctx.facts or SetupFacts()
        stripped = (command or "").strip()
        if (
            stage != SetupStage.INSTALL_OR_SKIP
            or not facts.is_live
            or facts.is_root
            or stripped.startswith("sudo ")
        ):
            return command
        rewritten = ""
        if re.match(r"test\s+-[a-zA-Z]+\s+/mnt/", stripped):
            rewritten = f"sudo {stripped}"
        else:
            bracket_match = re.fullmatch(r"\[\s+(-[a-zA-Z]+)\s+(/mnt/[^\]\s]+)\s+\]", stripped)
            if bracket_match:
                rewritten = (
                    f"sudo test {bracket_match.group(1)} {shlex.quote(bracket_match.group(2))}"
                )
        if not rewritten:
            return command
        self.runlog.record(
            "live_target_path_verification_normalized",
            stage=stage.value,
            original=command,
            rewritten=rewritten,
            reason="live non-root users may not traverse target-owned /mnt paths",
        )
        return rewritten

    @staticmethod
    def _live_chroot_tool_for_family(tool: str, facts: SetupFacts) -> str:
        normalized = (tool or "").lower()
        if normalized == "arch-chroot" and facts.package_manager != "pacman":
            return "chroot"
        return tool

    def _normalize_debian_live_efi_layout(
        self, stage: SetupStage, group: CommandGroup, commands: list[str]
    ) -> list[str]:
        facts = self.ctx.facts or SetupFacts()
        if (
            stage != SetupStage.INSTALL_OR_SKIP
            or not facts.is_live
            or facts.package_manager != "apt"
        ):
            return commands
        patched: list[str] = []
        for command in commands:
            rewritten = command
            rewritten = re.sub(
                r"((?:sudo\s+)?mkdir\s+-p\s+)/mnt/boot(?!/)\b",
                r"\1/mnt/boot/efi",
                rewritten,
                flags=re.IGNORECASE,
            )
            rewritten = _DEBIAN_LIVE_BOOT_MOUNT_RE.sub(
                lambda match: f"{match.group('prefix')}/mnt/boot/efi",
                rewritten,
            )
            rewritten = re.sub(
                r"(?P<prefix>[\s'\"])/boot(?P<suffix>\s+vfat\b)",
                r"\g<prefix>/boot/efi\g<suffix>",
                rewritten,
                flags=re.IGNORECASE,
            )
            rewritten = rewritten.replace("--efi-directory=/boot ", "--efi-directory=/boot/efi ")
            rewritten = rewritten.replace("--efi-directory=/boot'", "--efi-directory=/boot/efi'")
            rewritten = rewritten.replace('--efi-directory=/boot"', '--efi-directory=/boot/efi"')
            if rewritten != command:
                self.runlog.record(
                    "debian_live_efi_layout_normalized",
                    stage=stage.value,
                    group_id=group.id,
                    original=command,
                    rewritten=rewritten,
                    reason="Debian/Ubuntu kernels need /boot on a symlink-capable root filesystem; mount the ESP at /boot/efi",
                )
            patched.append(rewritten)
        return patched

    def _inject_target_home_writability_prep(
        self, stage: SetupStage, group: CommandGroup, commands: list[str]
    ) -> list[str]:
        facts = self.ctx.facts or SetupFacts()
        if stage not in {SetupStage.DESKTOP, SetupStage.APPS}:
            return commands
        if facts.package_manager != "apt":
            return commands
        if facts.is_live or facts.is_root or not (facts.login_user or facts.current_user):
            return commands
        if not commands:
            return commands
        prep = (
            'test -w "$HOME" || sudo chown "$USER:$USER" "$HOME"; '
            'mkdir -p "$HOME/.config" "$HOME/.local" "$HOME/snap"'
        )
        if any(command.strip() == prep for command in commands):
            return commands
        self.runlog.record(
            "target_home_writability_prep_injected",
            stage=stage.value,
            group_id=group.id,
            command=prep,
        )
        return [prep, *commands]

    def _inject_apt_external_repo_prereqs(
        self, stage: SetupStage, group: CommandGroup, commands: list[str]
    ) -> list[str]:
        facts = self.ctx.facts or SetupFacts()
        if facts.package_manager != "apt" or stage not in _INSTALLED_NONROOT_PRIVILEGED_STAGES:
            return commands
        if not any(_APT_EXTERNAL_REPO_SETUP_RE.search(command or "") for command in commands):
            return commands
        prep = "sudo apt update && sudo apt install -y curl ca-certificates gnupg"
        if any("curl ca-certificates gnupg" in command for command in commands):
            return commands
        self.runlog.record(
            "apt_external_repo_prereqs_injected",
            stage=stage.value,
            group_id=group.id,
            command=prep,
        )
        return [prep, *commands]

    def _inject_ubuntu_apt_component_prereqs(
        self, stage: SetupStage, group: CommandGroup, commands: list[str]
    ) -> list[str]:
        facts = self.ctx.facts or SetupFacts()
        is_ubuntu = facts.os_id.lower() == "ubuntu" or facts.distro_family.lower() == "ubuntu"
        if (
            facts.package_manager != "apt"
            or not is_ubuntu
            or stage not in _INSTALLED_NONROOT_PRIVILEGED_STAGES
            or not any(_APT_INSTALL_COMMAND_RE.search(command or "") for command in commands)
        ):
            return commands
        if any("quickhatch-ubuntu-components.list" in command for command in commands):
            return commands
        prep = (
            "sudo sh -c 'set -e; "
            'if grep -RqsE "^[[:space:]]*(deb|Components:).*(universe|multiverse)" '
            "/etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null; then exit 0; fi; "
            '. /etc/os-release; codename="${VERSION_CODENAME:-}"; '
            'if [ -z "$codename" ] && command -v lsb_release >/dev/null 2>&1; '
            'then codename="$(lsb_release -sc)"; fi; '
            '[ -n "$codename" ]; '
            'printf "%s\\n" '
            '"deb http://archive.ubuntu.com/ubuntu $codename restricted universe multiverse" '
            '"deb http://archive.ubuntu.com/ubuntu $codename-updates restricted universe multiverse" '
            '"deb http://archive.ubuntu.com/ubuntu $codename-backports restricted universe multiverse" '
            '"deb http://security.ubuntu.com/ubuntu $codename-security restricted universe multiverse" '
            "> /etc/apt/sources.list.d/quickhatch-ubuntu-components.list' && sudo apt-get update"
        )
        self.runlog.record(
            "ubuntu_apt_component_prereqs_injected",
            stage=stage.value,
            group_id=group.id,
            command=prep,
        )
        return [prep, *commands]

    def _normalize_apt_network_timeouts(
        self, stage: SetupStage, group: CommandGroup, commands: list[str]
    ) -> list[str]:
        facts = self.ctx.facts or SetupFacts()
        if facts.package_manager != "apt":
            return commands
        patched: list[str] = []
        for command in commands:

            def repl(match: re.Match[str], command: str = command) -> str:
                if self._apt_invocation_is_command_probe(command, match):
                    return match.group(0)
                return f"{match.group('tool')} {_APT_NETWORK_OPTIONS} "

            rewritten = _APT_COMMAND_RE.sub(
                repl,
                command,
            )
            if rewritten != command:
                self.runlog.record(
                    "apt_network_timeouts_injected",
                    stage=stage.value,
                    group_id=group.id,
                    original=command,
                    rewritten=rewritten,
                )
            patched.append(rewritten)
        return patched

    @staticmethod
    def _apt_invocation_is_command_probe(command: str, match: re.Match[str]) -> bool:
        prefix = command[: match.start()]
        segment = re.split(r"[;&|]", prefix)[-1].strip().lower()
        return segment.endswith("command -v") or segment.endswith("which")

    def _normalize_apt_noninteractive(
        self, stage: SetupStage, group: CommandGroup, commands: list[str]
    ) -> list[str]:
        facts = self.ctx.facts or SetupFacts()
        if facts.package_manager != "apt":
            return commands
        patched: list[str] = []
        for command in commands:

            def repl(match: re.Match[str], command: str = command) -> str:
                matched = match.group(0)
                if "DEBIAN_FRONTEND=" in matched or self._apt_invocation_has_debian_frontend(
                    command, match
                ):
                    return matched
                sudo = match.group("sudo") or ""
                tool = match.group("tool")
                tail = match.group("tail") or ""
                if sudo:
                    return f"{sudo}env DEBIAN_FRONTEND=noninteractive {tool}{tail}"
                return f"DEBIAN_FRONTEND=noninteractive {tool}{tail}"

            rewritten = _APT_MUTATING_COMMAND_RE.sub(repl, command)
            if rewritten != command:
                self.runlog.record(
                    "apt_noninteractive_injected",
                    stage=stage.value,
                    group_id=group.id,
                    original=command,
                    rewritten=rewritten,
                )
            patched.append(rewritten)
        return patched

    @staticmethod
    def _apt_invocation_has_debian_frontend(command: str, match: re.Match[str]) -> bool:
        prefix = command[: match.start()]
        segment = re.split(r"[;&|]", prefix)[-1].lower()
        return "debian_frontend=" in segment

    def _inject_debian_live_debootstrap_prereqs(
        self, stage: SetupStage, group: CommandGroup, commands: list[str]
    ) -> list[str]:
        facts = self.ctx.facts or SetupFacts()
        if (
            stage != SetupStage.INSTALL_OR_SKIP
            or not facts.is_live
            or facts.package_manager != "apt"
            or not any(_DEBIAN_LIVE_DEBOOTSTRAP_RE.search(command or "") for command in commands)
        ):
            return commands
        if any(
            "apt-get install" in (command or "").lower()
            and "debootstrap" in (command or "").lower()
            for command in commands
        ):
            return commands
        prep = (
            "sudo apt-get update && sudo apt-get install -y "
            "debootstrap ca-certificates dosfstools e2fsprogs parted"
        )
        self.runlog.record(
            "debian_live_debootstrap_prereqs_injected",
            stage=stage.value,
            group_id=group.id,
            command=prep,
        )
        return [prep, *commands]

    def _prefer_debian_live_cdrom_debootstrap_mirror(
        self, stage: SetupStage, group: CommandGroup, commands: list[str]
    ) -> list[str]:
        facts = self.ctx.facts or SetupFacts()
        if (
            stage != SetupStage.INSTALL_OR_SKIP
            or not facts.is_live
            or facts.package_manager != "apt"
        ):
            return commands
        patched: list[str] = []
        for command in commands:
            rewritten = self._rewrite_debootstrap_http_mirror_to_cdrom_fallback(command)
            if rewritten != command:
                self.runlog.record(
                    "debian_live_cdrom_debootstrap_mirror_preferred",
                    stage=stage.value,
                    group_id=group.id,
                    original=command,
                    rewritten=rewritten,
                )
            patched.append(rewritten)
        return patched

    @staticmethod
    def _rewrite_debootstrap_http_mirror_to_cdrom_fallback(command: str) -> str:
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            return command
        debootstrap_index = next(
            (
                index
                for index, token in enumerate(tokens)
                if token.rstrip().split("/")[-1] == "debootstrap"
            ),
            -1,
        )
        if debootstrap_index < 0:
            return command
        if SetupOrchestrator._debootstrap_is_inside_complex_shell(tokens, debootstrap_index):
            return SetupOrchestrator._normalize_debootstrap_mirrors_in_shell(command)

        positional: list[tuple[int, str]] = []
        index = debootstrap_index + 1
        while index < len(tokens):
            token = tokens[index]
            if token == "--":
                index += 1
                continue
            if token.startswith("--"):
                option = token.split("=", 1)[0]
                if "=" not in token and option in _DEBOOTSTRAP_OPTION_ARGS_WITH_VALUE:
                    index += 2
                    continue
                index += 1
                continue
            if token.startswith("-"):
                if token in _DEBOOTSTRAP_OPTION_ARGS_WITH_VALUE:
                    index += 2
                    continue
                index += 1
                continue
            positional.append((index, token))
            index += 1

        if len(positional) < 2:
            return command
        suite = positional[0][1]
        mirror_index = positional[2][0] if len(positional) >= 3 else -1
        original_mirror_index = mirror_index
        mirror = positional[2][1] if len(positional) >= 3 else ""
        mirror_is_rewritable = mirror.startswith(("http://", "https://")) or mirror in {
            "$MIRROR",
            "${MIRROR}",
        }
        if mirror and not mirror_is_rewritable:
            return command

        cdrom_tokens = list(tokens)
        if "--no-check-gpg" not in cdrom_tokens:
            cdrom_tokens.insert(debootstrap_index + 1, "--no-check-gpg")
            if mirror_index >= 0:
                mirror_index += 1
        if mirror_index >= 0:
            cdrom_tokens[mirror_index] = "file:///cdrom"
        else:
            cdrom_tokens.append("file:///cdrom")
        fallback_tokens = list(tokens)
        if original_mirror_index >= 0 and mirror in {"$MIRROR", "${MIRROR}"}:
            fallback_tokens[original_mirror_index] = (
                SetupOrchestrator._first_http_url_in_text(command)
                or "http://archive.ubuntu.com/ubuntu"
            )
        if original_mirror_index >= 0:
            fallback_tokens[original_mirror_index] = (
                SetupOrchestrator._normalize_debootstrap_fallback_mirror(
                    suite, fallback_tokens[original_mirror_index], command
                )
            )
        suite_path = f"/cdrom/dists/{suite}"
        cdrom_complete_check = (
            f"[ -d {shlex.quote(suite_path)} ] && qh_cdrom_ok=1 && "
            "for qh_pkg in base-files base-passwd bash coreutils dpkg libc6; do "
            'find /cdrom/pool -type f -name "${qh_pkg}_*.deb" -print -quit | '
            "grep -q . || qh_cdrom_ok=0; "
            'done && [ "$qh_cdrom_ok" = 1 ]'
        )
        return (
            f"if {cdrom_complete_check}; then "
            f"{shlex.join(cdrom_tokens)}; "
            f"else {shlex.join(fallback_tokens)}; fi"
        )

    @staticmethod
    def _first_http_url_in_text(text: str) -> str:
        match = re.search(r"https?://[^\s\"')]+", text or "")
        return match.group(0) if match else ""

    @staticmethod
    def _debootstrap_is_inside_complex_shell(tokens: list[str], debootstrap_index: int) -> bool:
        before = [token.rstrip(";").lower() for token in tokens[:debootstrap_index]]
        return any(token in _DEBOOTSTRAP_SHELL_CONTROL_TOKENS for token in before) or any(
            token.endswith(";") or token in {"&&", "||", "|"} for token in tokens
        )

    @staticmethod
    def _normalize_debootstrap_mirrors_in_shell(command: str) -> str:
        fallback_mirror = SetupOrchestrator._infer_debootstrap_fallback_mirror(command)
        rewritten = re.sub(
            r"https?://(?:archive|security)\.ubuntu\.com/ubuntu\b",
            _UBUNTU_DEBOOTSTRAP_FALLBACK_MIRROR,
            command,
            flags=re.IGNORECASE,
        )
        rewritten = re.sub(
            r"https?://deb\.debian\.org/debian\b",
            _DEBIAN_DEBOOTSTRAP_FALLBACK_MIRROR,
            rewritten,
            flags=re.IGNORECASE,
        )
        rewritten = re.sub(r"file:///cdrom\b", fallback_mirror, rewritten, flags=re.IGNORECASE)
        return rewritten

    @staticmethod
    def _infer_debootstrap_fallback_mirror(command: str) -> str:
        lower = (command or "").lower()
        ubuntu_codenames = (
            "resolute",
            "questing",
            "plucky",
            "oracular",
            "noble",
            "mantic",
            "lunar",
            "kinetic",
            "jammy",
            "impish",
            "hirsute",
            "focal",
            "bionic",
            "xenial",
            "trusty",
            "gutsy",
        )
        if "ubuntu" in lower or any(re.search(rf"\b{name}\b", lower) for name in ubuntu_codenames):
            return _UBUNTU_DEBOOTSTRAP_FALLBACK_MIRROR
        return _DEBIAN_DEBOOTSTRAP_FALLBACK_MIRROR

    @staticmethod
    def _normalize_debootstrap_fallback_mirror(suite: str, mirror: str, command: str) -> str:
        normalized = (mirror or "").strip()
        haystack = f"{suite} {mirror} {command}".lower()
        if "ubuntu" in haystack and any(
            host in normalized
            for host in (
                "archive.ubuntu.com/ubuntu",
                "security.ubuntu.com/ubuntu",
                "ports.ubuntu.com/ubuntu-ports",
            )
        ):
            return _UBUNTU_DEBOOTSTRAP_FALLBACK_MIRROR
        if "debian" in haystack and "deb.debian.org/debian" not in normalized:
            return _DEBIAN_DEBOOTSTRAP_FALLBACK_MIRROR
        return normalized

    def _normalize_live_resolv_conf_copies(
        self, stage: SetupStage, group: CommandGroup, commands: list[str]
    ) -> list[str]:
        facts = self.ctx.facts or SetupFacts()
        if stage != SetupStage.INSTALL_OR_SKIP or not facts.is_live:
            return commands
        patched: list[str] = []
        for command in commands:
            rewritten = self._rewrite_live_resolv_conf_copy(command)
            if rewritten != command:
                self.runlog.record(
                    "live_resolv_conf_copy_normalized",
                    stage=stage.value,
                    group_id=group.id,
                    original=command,
                    rewritten=rewritten,
                )
            patched.append(rewritten)
        return patched

    @staticmethod
    def _rewrite_live_resolv_conf_copy(command: str) -> str:
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            return command
        if tokens and tokens[0] == "sudo":
            tokens = tokens[1:]
        if not tokens or tokens[0] != "cp":
            return command
        args = [token for token in tokens[1:] if token not in {"-L", "-f"}]
        if (
            len(args) != 2
            or args[0] != "/etc/resolv.conf"
            or args[1] not in _LIVE_RESOLV_CONF_TARGETS
        ):
            return command
        target_q = shlex.quote(args[1])
        return "sudo sh -c " + shlex.quote(
            "if ! cmp -s /etc/resolv.conf "
            f"{target_q} 2>/dev/null; then "
            f"rm -f {target_q} && cp -L /etc/resolv.conf {target_q}; "
            "fi"
        )

    def _inject_fedora_live_shadow_lock_cleanup(
        self, stage: SetupStage, group: CommandGroup, commands: list[str]
    ) -> list[str]:
        facts = self.ctx.facts or SetupFacts()
        if (
            stage != SetupStage.INSTALL_OR_SKIP
            or not facts.is_live
            or facts.distro_family != "fedora"
            or not any(
                re.search(r"\b(?:chpasswd|passwd|useradd|groupadd)\b", command or "")
                for command in commands
            )
        ):
            return commands
        needs_setenforce = not any("setenforce" in (command or "") for command in commands)
        needs_lock_cleanup = not any(".pwd.lock" in (command or "") for command in commands)
        if not needs_setenforce and not needs_lock_cleanup:
            return commands
        setenforce = "sudo setenforce 0 || true"
        cleanup = (
            "sudo rm -f /mnt/etc/.pwd.lock /mnt/etc/passwd.lock /mnt/etc/shadow.lock "
            "/mnt/etc/group.lock /mnt/etc/gshadow.lock /mnt/etc/passwd+ /mnt/etc/shadow+ "
            "/mnt/etc/group+ /mnt/etc/gshadow+"
        )
        patched: list[str] = []
        injected = False
        for command in commands:
            if not injected and re.search(
                r"\b(?:chpasswd|passwd|useradd|groupadd)\b", command or ""
            ):
                if needs_setenforce:
                    patched.append(setenforce)
                    self.runlog.record(
                        "fedora_live_selinux_permissive_injected",
                        stage=stage.value,
                        group_id=group.id,
                        command=setenforce,
                        reason="Fedora live SELinux can prevent chroot user/password tools from writing target shadow files",
                    )
                if needs_lock_cleanup:
                    patched.append(cleanup)
                    self.runlog.record(
                        "fedora_live_shadow_lock_cleanup_injected",
                        stage=stage.value,
                        group_id=group.id,
                        command=cleanup,
                        reason="Fedora installroot user tools can leave /etc/.pwd.lock after partial user creation",
                    )
                injected = True
            patched.append(command)
        return patched

    def _inject_debian_live_chroot_bind_mounts(
        self, stage: SetupStage, group: CommandGroup, commands: list[str]
    ) -> list[str]:
        facts = self.ctx.facts or SetupFacts()
        if (
            stage != SetupStage.INSTALL_OR_SKIP
            or not facts.is_live
            or facts.package_manager != "apt"
        ):
            return commands
        patched: list[str] = []
        injected_targets: set[str] = set()
        for command in commands:
            match = _DEBIAN_LIVE_CHROOT_RE.search(command or "")
            if match:
                target = match.group("target")
                if target not in injected_targets:
                    injected_targets.add(target)
                    target_q = shlex.quote(target)
                    prep = "sudo sh -c " + shlex.quote(
                        f"target={target_q}; "
                        'test -d "$target/etc" && cp -L /etc/resolv.conf "$target/etc/resolv.conf" || true; '
                        "for d in dev dev/pts proc sys run; do "
                        'mkdir -p "$target/$d"; '
                        'mountpoint -q "$target/$d" || mount --bind "/$d" "$target/$d"; '
                        "done"
                    )
                    patched.append(prep)
                    self.runlog.record(
                        "debian_live_chroot_bind_mounts_injected",
                        stage=stage.value,
                        group_id=group.id,
                        target=target,
                        command=prep,
                    )
            patched.append(command)
        return patched

    def _normalize_fedora_kickstart_command(self, command: str) -> str:
        facts = self.ctx.facts or SetupFacts()
        if facts.distro_family != "fedora" and facts.os_id != "fedora":
            return command
        normalized = command
        if "printf" in normalized and ".ks" in normalized:
            normalized = _KICKSTART_DIRECTIVE_IN_PRINTF_RE.sub(r"%%\1", normalized)
        version_id = str(facts.raw.get("QH_OS_VERSION_ID", "") or "").strip()
        if version_id:
            normalized = normalized.replace("repo=fedora-$releasever", f"repo=fedora-{version_id}")
            normalized = normalized.replace(
                "repo=fedora-${releasever}", f"repo=fedora-{version_id}"
            )
        return normalized

    def _normalize_fedora_dnf_config_manager_command(self, command: str) -> str:
        facts = self.ctx.facts or SetupFacts()
        if facts.distro_family != "fedora" and facts.os_id != "fedora":
            return command
        stripped = (command or "").strip()
        version_id = str(facts.raw.get("QH_OS_VERSION_ID", "") or "").strip()
        version_match = re.match(r"(\d+)", version_id)
        dnf5_era = bool(version_match and int(version_match.group(1)) >= 41)
        if "dnf5" not in stripped.lower() and not dnf5_era:
            return command
        match = _DNF_CONFIG_MANAGER_ADD_REPO_RE.match(command or "")
        if not match:
            return command
        return (
            f"{match.group('prefix')}addrepo --from-repofile "
            f"{match.group('url')}{match.group('rest')}"
        )

    def _normalize_fedora_virtualbox_service_command(
        self, command: str, *, stage: SetupStage, group: CommandGroup
    ) -> str:
        facts = self.ctx.facts or SetupFacts()
        if facts.distro_family != "fedora" and facts.os_id != "fedora":
            return command
        rewritten = re.sub(r"\bvboxadd(?:\.service)?\b", "vboxservice", command or "")
        if rewritten != command:
            self.runlog.record(
                "fedora_virtualbox_service_normalized",
                stage=stage.value,
                group_id=group.id,
                original=command,
                rewritten=rewritten,
                reason="Fedora virtualbox-guest-additions provides vboxservice.service, not vboxadd.service",
            )
        return rewritten

    def _normalize_fedora_removed_package_command(
        self, command: str, *, stage: SetupStage, group: CommandGroup
    ) -> str:
        facts = self.ctx.facts or SetupFacts()
        if facts.distro_family != "fedora" and facts.os_id != "fedora":
            return command
        lower = (command or "").lower()
        if "dnf" not in lower or "install" not in lower or "gnome-themes-extra" not in lower:
            return command
        rewritten = re.sub(r"(?<!\S)gnome-themes-extra(?!\S)\s*", "", command).strip()
        if rewritten != command:
            self.runlog.record(
                "fedora_removed_package_dropped",
                stage=stage.value,
                group_id=group.id,
                original=command,
                rewritten=rewritten,
                package="gnome-themes-extra",
                reason="gnome-themes-extra is not available on current Fedora releases; GTK/Adwaita theming lives in other packages",
            )
        return rewritten

    def _normalize_root_package_manager_run_as_target_user(
        self, command: str, *, stage: SetupStage, group: CommandGroup
    ) -> str:
        if any(operator in (command or "") for operator in ("&&", "||", ";", "|", "\n")):
            return command
        try:
            tokens = shlex.split(command or "", posix=True)
        except ValueError:
            return command
        if not tokens or tokens[0] != "sudo":
            return command
        idx = 1
        preserved_sudo_options: list[str] = []
        target_user = ""
        while idx < len(tokens):
            token = tokens[idx]
            if token in {"-u", "--user"} and idx + 1 < len(tokens):
                target_user = tokens[idx + 1]
                idx += 2
                break
            if token.startswith("--user="):
                target_user = token.split("=", 1)[1]
                idx += 1
                break
            if token.startswith("-"):
                preserved_sudo_options.append(token)
                idx += 1
                continue
            return command
        if not target_user or idx >= len(tokens):
            return command
        payload = tokens[idx:]
        tool = self._payload_root_package_tool(payload)
        if not tool or not self._payload_package_tool_mutates(tool, payload):
            return command
        facts = self.ctx.facts or SetupFacts()
        if facts.is_root or (facts.current_user or facts.login_user) == "root":
            rewritten_tokens = payload
        else:
            rewritten_tokens = ["sudo", *preserved_sudo_options, *payload]
        rewritten = shlex.join(rewritten_tokens)
        if rewritten != command:
            self.runlog.record(
                "root_package_manager_user_wrapper_normalized",
                stage=stage.value,
                group_id=group.id,
                original=command,
                rewritten=rewritten,
                package_manager_tool=tool,
                target_user=target_user,
                reason=(
                    "Root package manager mutation was wrapped in sudo -u target user; "
                    "run it with root privileges instead."
                ),
            )
        return rewritten

    @staticmethod
    def _payload_root_package_tool(tokens: list[str]) -> str:
        idx = 0
        while idx < len(tokens):
            token = tokens[idx]
            if token == "env" or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", token):
                idx += 1
                continue
            return token.rsplit("/", 1)[-1]
        return ""

    @staticmethod
    def _payload_package_tool_mutates(tool: str, tokens: list[str]) -> bool:
        normalized = tool.lower()
        if normalized == "pacman":
            return any(token.startswith(("-S", "-R", "-U", "-D")) for token in tokens[1:])
        mutating_subcommands = {
            "install",
            "remove",
            "erase",
            "upgrade",
            "full-upgrade",
            "dist-upgrade",
            "autoremove",
            "update",
            "add",
            "del",
            "sync",
            "merge",
        }
        if normalized in {"apt", "apt-get", "dnf", "zypper", "apk", "xbps-install", "emerge"}:
            return any(token.lower() in mutating_subcommands for token in tokens[1:])
        return False

    @staticmethod
    def _normalize_mountpoint_probe_command(command: str) -> str:
        return re.sub(r"\bmount\s+--point\b", "mountpoint -q", command or "")

    @staticmethod
    def _combine_standalone_assignments(commands: list[str]) -> list[str]:
        combined: list[str] = []
        pending_assignment = ""
        pending_variables: set[str] = set()
        pending_separator = " && "
        for command in commands:
            stripped = (command or "").strip()
            if pending_assignment:
                if not pending_variables or SetupOrchestrator._references_any_variable(
                    stripped, pending_variables
                ):
                    combined.append(f"{pending_assignment}{pending_separator}{stripped}")
                else:
                    combined.append(pending_assignment)
                    combined.append(command)
                pending_assignment = ""
                pending_variables = set()
                pending_separator = " && "
                continue
            if _STANDALONE_ASSIGNMENT_RE.fullmatch(stripped):
                pending_assignment = stripped
                pending_variables = set()
                pending_separator = " && "
                continue
            stateful_variables = SetupOrchestrator._stateful_assignment_prelude_variables(stripped)
            if stateful_variables:
                pending_assignment = stripped.rstrip(";")
                pending_variables = stateful_variables
                pending_separator = "; "
                continue
            combined.append(command)
        if pending_assignment:
            combined.append(pending_assignment)
        return combined

    @staticmethod
    def _stateful_assignment_prelude_variables(command: str) -> set[str]:
        """Detect shell-state setup that must be joined to the next SSH command.

        Examples include `DISK=$(...) && [ -n "$DISK" ] || DISK=/dev/sda`.
        This is not a mutating command by itself, but running it in a separate
        SSH session drops the variable and breaks the following command.
        """

        stripped = (command or "").strip()
        if not _STATEFUL_ASSIGNMENT_PREFIX_RE.match(stripped):
            return set()
        variables = {match.group("name") for match in _ASSIGNMENT_NAME_RE.finditer(stripped)}
        if not variables or _STANDALONE_ASSIGNMENT_RE.fullmatch(stripped):
            return set()
        surface = SetupOrchestrator._strip_command_substitutions(stripped)
        surface = re.sub(r"'[^']*'|\"[^\"]*\"", "", surface)
        if _STATEFUL_ASSIGNMENT_REAL_COMMAND_RE.search(surface):
            return set()
        return variables

    @staticmethod
    def _strip_command_substitutions(command: str) -> str:
        output: list[str] = []
        index = 0
        length = len(command or "")
        while index < length:
            if command.startswith("$(", index):
                depth = 1
                index += 2
                while index < length and depth:
                    if command.startswith("$(", index):
                        depth += 1
                        index += 2
                        continue
                    if command[index] == ")":
                        depth -= 1
                    index += 1
                output.append("$(...)")
                continue
            output.append(command[index])
            index += 1
        return "".join(output)

    @staticmethod
    def _references_any_variable(command: str, variables: set[str]) -> bool:
        for variable in variables:
            if re.search(rf"\$(?:\{{{re.escape(variable)}\}}|{re.escape(variable)}\b)", command):
                return True
        return False

    def _wrap_desktop_runtime_command(self, command: str, *, stage: SetupStage) -> str:
        if stage != SetupStage.DESKTOP or not self._command_needs_desktop_runtime_env(command):
            return command
        facts = self.ctx.facts or SetupFacts()
        gui_user = (
            str(facts.raw.get("QH_GUI_USER", "") or "").strip()
            or facts.login_user
            or facts.current_user
            or self.runtime.user
        )
        current_user = facts.current_user or self.runtime.user
        user_q = shlex.quote(gui_user)
        inner = (
            "qh_pid=$(for qh_proc in plasmashell kwin_wayland kwin_x11 "
            "gnome-shell cinnamon-session xfce4-session mate-session lxqt-session; "
            f'do qh_found=$(pgrep -u {user_q} -n "$qh_proc" 2>/dev/null || true); '
            'if [ -n "$qh_found" ]; then printf \'%s\\n\' "$qh_found"; break; fi; '
            "done); "
            'if [ -n "$qh_pid" ] && [ -r "/proc/$qh_pid/environ" ]; then '
            'while IFS= read -r qh_env; do case "$qh_env" in '
            "DISPLAY=*|WAYLAND_DISPLAY=*|XAUTHORITY=*|XDG_RUNTIME_DIR=*|"
            "DBUS_SESSION_BUS_ADDRESS=*|XDG_CURRENT_DESKTOP=*|XDG_SESSION_TYPE=*) "
            'export "$qh_env";; esac; done < <(tr "\\000" "\\n" < "/proc/$qh_pid/environ"); '
            "fi; "
            'export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-wayland}"; '
            f"{command}"
        )
        wrapped = f"bash -lc {shlex.quote(inner)}"
        if current_user and gui_user and current_user != gui_user and not facts.is_root:
            return wrapped
        if facts.is_root and gui_user and gui_user != "root":
            return f"sudo -u {user_q} {wrapped}"
        return wrapped

    @staticmethod
    def _command_needs_desktop_runtime_env(command: str) -> bool:
        return bool(_DESKTOP_RUNTIME_COMMAND_RE.search(command or ""))

    def _drop_live_install_harness_owned_commands(
        self, stage: SetupStage, group: CommandGroup
    ) -> list[str]:
        commands: list[str] = []
        facts = self.ctx.facts or SetupFacts()
        for command in group.commands:
            if (
                stage == SetupStage.INSTALL_OR_SKIP
                and facts.is_live
                and (
                    self._is_reboot_command(command)
                    or self._is_live_target_unmount_command(command)
                )
            ):
                self.runlog.record(
                    "command_deferred_until_guarded_reboot",
                    stage=stage.value,
                    group_id=group.id,
                    command=command,
                    reason="live install unmount/reboot is harness-owned",
                )
                continue
            commands.append(command)
        return commands

    def _inject_arch_live_vconsole_before_pacstrap(
        self, stage: SetupStage, group: CommandGroup, commands: list[str]
    ) -> list[str]:
        facts = self.ctx.facts or SetupFacts()
        if (
            stage != SetupStage.INSTALL_OR_SKIP
            or not facts.is_live
            or facts.distro_family != "arch"
            or any("vconsole.conf" in command for command in commands)
        ):
            return commands

        patched: list[str] = []
        injected = False
        for command in commands:
            if not injected and _ARCH_PACSTRAP_MNT_RE.search(command):
                patched.append(_ARCH_LIVE_VCONSOLE_PREP_COMMAND)
                injected = True
                self.runlog.record(
                    "arch_live_vconsole_prep_injected",
                    stage=stage.value,
                    group_id=group.id,
                    command=_ARCH_LIVE_VCONSOLE_PREP_COMMAND,
                )
            patched.append(command)
        return patched

    def _inject_arch_live_keyring_before_pacstrap(
        self, stage: SetupStage, group: CommandGroup, commands: list[str]
    ) -> list[str]:
        facts = self.ctx.facts or SetupFacts()
        if (
            stage != SetupStage.INSTALL_OR_SKIP
            or not facts.is_live
            or facts.package_manager != "pacman"
            or facts.distro_family != "arch"
            or not any(_ARCH_PACSTRAP_MNT_RE.search(command or "") for command in commands)
            or any(
                "archlinux-keyring" in (command or "").lower()
                or "pacman-key" in (command or "").lower()
                for command in commands
            )
        ):
            return commands

        patched: list[str] = []
        injected = False
        for command in commands:
            if not injected and _ARCH_PACSTRAP_MNT_RE.search(command or ""):
                patched.append(_ARCH_LIVE_PACSTRAP_KEYRING_COMMAND)
                injected = True
                self.runlog.record(
                    "arch_live_keyring_prep_injected",
                    stage=stage.value,
                    group_id=group.id,
                    command=_ARCH_LIVE_PACSTRAP_KEYRING_COMMAND,
                    reason="fresh Arch live media often needs a current keyring before pacstrap can verify packages",
                )
            patched.append(command)
        return patched

    def _normalize_installed_nonroot_privileged_command(
        self, command: str, *, stage: SetupStage
    ) -> str:
        facts = self.ctx.facts or SetupFacts()
        if stage not in _INSTALLED_NONROOT_PRIVILEGED_STAGES or facts.is_live:
            return command
        stripped = command.strip()
        chroot_stripped = self._strip_installed_chroot_prefix(stripped)
        if chroot_stripped != stripped:
            self.runlog.record(
                "command_installed_chroot_stripped",
                stage=stage.value,
                original=command,
                rewritten=chroot_stripped,
            )
            stripped = chroot_stripped
        if facts.is_root:
            return stripped if stripped != command.strip() else command
        same_user_stripped = self._strip_same_user_execution_wrapper(stripped)
        if same_user_stripped != stripped:
            self.runlog.record(
                "command_same_user_wrapper_stripped",
                stage=stage.value,
                original=command,
                rewritten=same_user_stripped,
            )
            stripped = same_user_stripped
        rewritten = self._sudo_tee_for_privileged_redirection(stripped)
        if rewritten != stripped:
            self.runlog.record(
                "command_privilege_normalized",
                stage=stage.value,
                original=command,
                rewritten=rewritten,
                reason="root-owned shell redirection",
            )
            return rewritten
        if not stripped or self._has_privileged_command_wrapper(stripped):
            return stripped if stripped != command.strip() else command
        if self._uses_arch_community_helper_command(stripped):
            return stripped if stripped != command.strip() else command

        rewritten = self._sudo_tee_for_privileged_pipeline(stripped)
        if rewritten != stripped:
            self.runlog.record(
                "command_privilege_normalized",
                stage=stage.value,
                original=command,
                rewritten=rewritten,
                reason="privileged tee pipeline",
            )
            return rewritten

        if _PRIVILEGED_COMMAND_RE.search(stripped) or self._writes_privileged_path(stripped):
            rewritten = f"sudo {stripped}"
            self.runlog.record(
                "command_privilege_normalized",
                stage=stage.value,
                original=command,
                rewritten=rewritten,
                reason="installed non-root privileged command",
            )
            return rewritten
        return stripped if stripped != command.strip() else command

    @staticmethod
    def _strip_installed_chroot_prefix(command: str) -> str:
        match = _INSTALLED_CHROOT_PREFIX_RE.match(command.strip())
        if not match:
            return command
        inner = match.group("inner").strip()
        if inner.startswith("/bin/bash "):
            inner = "bash " + inner[len("/bin/bash ") :]
        return inner

    @staticmethod
    def _has_privileged_command_wrapper(command: str) -> bool:
        return bool(
            re.match(r"^(?:sudo|doas|su|runuser|arch-chroot|chroot)\b", command)
            or re.search(r"(?:^|[;&|]\s*)(?:sudo|doas)\s+", command)
        )

    def _strip_same_user_execution_wrapper(self, command: str) -> str:
        facts = self.ctx.facts or SetupFacts()
        current_user = (facts.current_user or facts.login_user or self.runtime.user or "").strip()
        if not current_user:
            return command
        literal_sudo = re.match(
            rf"^\s*sudo\s+(?:-u|--user)\s+{re.escape(current_user)}\s+(?P<rest>.+)$",
            command,
            re.DOTALL,
        )
        if literal_sudo:
            return literal_sudo.group("rest").strip()
        literal_sudo_equals = re.match(
            rf"^\s*sudo\s+--user={re.escape(current_user)}\s+(?P<rest>.+)$",
            command,
            re.DOTALL,
        )
        if literal_sudo_equals:
            return literal_sudo_equals.group("rest").strip()
        literal_runuser = re.match(
            rf"^\s*runuser\s+(?:-u|--user)\s+{re.escape(current_user)}\s+--\s+(?P<rest>.+)$",
            command,
            re.DOTALL,
        )
        if literal_runuser:
            return literal_runuser.group("rest").strip()
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            return command
        if not tokens:
            return command
        tool = tokens[0]
        if tool == "runuser":
            idx = 1
            target_user = ""
            if idx < len(tokens) and tokens[idx] in {"-u", "--user"}:
                if idx + 1 >= len(tokens):
                    return command
                target_user = tokens[idx + 1]
                idx += 2
            elif idx < len(tokens) and tokens[idx].startswith("--user="):
                target_user = tokens[idx].split("=", 1)[1]
                idx += 1
            else:
                return command
            if idx < len(tokens) and tokens[idx] == "--":
                idx += 1
            if target_user == current_user and idx < len(tokens):
                return shlex.join(tokens[idx:])
            return command
        if tool == "sudo":
            idx = 1
            target_user = "root"
            while idx < len(tokens):
                token = tokens[idx]
                if token in {"-u", "--user"}:
                    if idx + 1 >= len(tokens):
                        return command
                    target_user = tokens[idx + 1]
                    idx += 2
                    continue
                if token.startswith("--user="):
                    target_user = token.split("=", 1)[1]
                    idx += 1
                    continue
                if token in {"-g", "-h", "-D", "-p", "-r", "-t", "-T", "-C", "-R"}:
                    idx += 2
                    continue
                if token.startswith("-"):
                    idx += 1
                    continue
                break
            if target_user == current_user and idx < len(tokens):
                return shlex.join(tokens[idx:])
        return command

    @staticmethod
    def _writes_privileged_path(command: str) -> bool:
        return bool(
            _PRIVILEGED_PATH_WRITE_RE.search(command) and _PRIVILEGED_PATH_RE.search(command)
        )

    @staticmethod
    def _sudo_tee_for_privileged_pipeline(command: str) -> str:
        if re.search(r"\|\s*sudo\s+tee\b", command):
            return command
        if not re.search(
            r"\|\s*tee\b(?:\s+-[A-Za-z]+)*\s+/(?:etc|boot|usr|var|opt)(?:/|\s|$)",
            command,
        ):
            return command
        return re.sub(r"\|\s*tee\b", "| sudo tee", command, count=1)

    @staticmethod
    def _is_privileged_redirect_target(path: str) -> bool:
        stripped = (path or "").strip().strip("'\"")
        return stripped in _PRIVILEGED_REDIRECT_EXACT_PATHS or any(
            stripped.startswith(prefix) for prefix in _PRIVILEGED_REDIRECT_PREFIXES
        )

    @staticmethod
    def _split_shell_redirection(command: str) -> tuple[str, str, str] | None:
        quote: str | None = None
        escaped = False
        idx = 0
        while idx < len(command):
            char = command[idx]
            if escaped:
                escaped = False
                idx += 1
                continue
            if char == "\\":
                escaped = True
                idx += 1
                continue
            if quote:
                if char == quote:
                    quote = None
                idx += 1
                continue
            if char in {"'", '"'}:
                quote = char
                idx += 1
                continue
            if char == ">":
                op = ">>" if idx + 1 < len(command) and command[idx + 1] == ">" else ">"
                rest = command[idx + len(op) :].strip()
                if not rest or rest.startswith("&"):
                    idx += len(op)
                    continue
                try:
                    target = shlex.split(rest, posix=True)[0]
                except (IndexError, ValueError):
                    target = rest.split(maxsplit=1)[0] if rest else ""
                if target:
                    return command[:idx].strip(), op, target
                idx += len(op)
                continue
            idx += 1
        return None

    @staticmethod
    def _strip_leading_sudo_for_redirection(tokens: list[str]) -> list[str]:
        if not tokens or tokens[0] != "sudo":
            return tokens
        idx = 1
        while idx < len(tokens):
            token = tokens[idx]
            if token in {"-u", "-g", "-h", "-D", "-p", "-r", "-t", "-T", "-C", "-R"}:
                idx += 2
                continue
            if token.startswith("-"):
                idx += 1
                continue
            break
        return tokens[idx:]

    def _sudo_tee_for_privileged_redirection(self, command: str) -> str:
        split = self._split_shell_redirection(command)
        if split is None:
            return command
        left, op, target = split
        if not self._is_privileged_redirect_target(target):
            return command
        if any(separator in left for separator in ("|", "&&", "||", ";")):
            return command
        try:
            tokens = shlex.split(left, posix=True)
        except ValueError:
            return command
        tokens = self._strip_leading_sudo_for_redirection(tokens)
        if not tokens:
            return command
        tool = tokens[0].split("/")[-1].lower()
        if tool not in {"echo", "printf"}:
            return command
        writer = shlex.join(tokens)
        tee = "tee -a" if op == ">>" else "tee"
        return f"{writer} | sudo {tee} {shlex.quote(target)} >/dev/null"

    def _is_arch_nvidia_driver_group(
        self, stage: SetupStage, group: CommandGroup, commands: list[str]
    ) -> bool:
        facts = self.ctx.facts or SetupFacts()
        return facts.package_manager == "pacman" and self._is_nvidia_driver_group(
            stage, group, commands
        )

    def _is_nvidia_driver_group(
        self, stage: SetupStage, group: CommandGroup, commands: list[str]
    ) -> bool:
        facts = self.ctx.facts or SetupFacts()
        if stage != SetupStage.POSTBOOT_DRIVER:
            return False
        if not self._has_nvidia_gpu(facts):
            return False
        text = " ".join([group.title, *group.packages, *commands]).lower()
        return any(token in text for token in ("nvidia", "nouveau", "ubuntu-drivers", "akmod"))

    def _normalize_arch_nvidia_commands(self, commands: list[str]) -> list[str]:
        if self._arch_nvidia_requires_manual_package_resolution():
            return commands
        normalized: list[str] = []
        inserted_install = False
        for command in commands:
            stripped = command.strip()
            lower = stripped.lower()
            if not stripped:
                continue
            if "export kernel_pkg" in lower or "export nv_pkg" in lower:
                continue
            if "$nv_pkg" in lower:
                if not inserted_install:
                    normalized.append(self._arch_nvidia_install_command())
                    inserted_install = True
                continue
            if _ARCH_NVIDIA_INSTALL_RE.search(stripped):
                if not inserted_install:
                    normalized.append(self._arch_nvidia_install_command())
                    inserted_install = True
                continue
            if "modprobe.d/nvidia.conf" in lower or "blacklist-nouveau" in lower:
                continue
            if "mkinitcpio" in lower and " -p" in lower:
                continue
            if re.search(r"\bmodprobe\s+nvidia(?:\b|_)", lower):
                continue
            normalized.append(stripped)
        if not inserted_install and any("nvidia" in command.lower() for command in commands):
            normalized.insert(0, self._arch_nvidia_install_command())
        for command in _ARCH_NVIDIA_BOOT_CONFIG_COMMANDS:
            if command not in normalized:
                normalized.append(command)
        for command in _ARCH_NVIDIA_MODULE_LOAD_COMMANDS:
            if command not in normalized:
                normalized.append(command)
        return normalized

    def _inject_nvidia_recovery_group_if_needed(self) -> None:
        facts = self.ctx.facts or SetupFacts()
        plan = self.ctx.plan
        if plan is None or not self._needs_nvidia_recovery_group(facts):
            return
        existing_groups = [
            group for group in plan.command_groups if group.stage == SetupStage.POSTBOOT_DRIVER
        ]
        if any(
            self._is_nvidia_driver_group(
                SetupStage.POSTBOOT_DRIVER, group, [*group.commands, *group.verification]
            )
            for group in existing_groups
        ):
            return
        group = self._nvidia_recovery_group(facts)
        if group is None:
            self._mark_nvidia_driver_manual_followup(facts)
            return
        plan.command_groups.append(group)
        packages = list(group.packages)
        plan.driver_strategy.strategy = group.id.replace("quickhatch-", "").replace("-recovery", "")
        plan.driver_strategy.confidence = (
            "resolved_community" if group.source == "community" else "resolved_official"
        )
        plan.driver_strategy.source = group.source
        plan.driver_strategy.packages = packages
        self.bridge.add_message(
            "agent",
            (
                "NVIDIA hardware is present but the researched plan has no postboot driver "
                f"group; injecting {self._nvidia_recovery_family_label(facts)} NVIDIA "
                "recovery commands before auditing drivers."
            ),
        )
        self.runlog.record(
            "nvidia_recovery_group_injected",
            stage=SetupStage.POSTBOOT_DRIVER.value,
            facts=asdict(facts),
            group=asdict(group),
            packages=packages,
        )

    def _needs_nvidia_recovery_group(self, facts: SetupFacts) -> bool:
        if not self._has_nvidia_gpu(facts):
            return False
        if (
            facts.package_manager == "pacman"
            and self._arch_nvidia_requires_manual_package_resolution()
        ):
            return False
        if facts.nvidia_nouveau_bound:
            return True
        if facts.nvidia_smi_state in {"missing", "present_but_failing"}:
            return True
        if facts.nvidia_kernel_driver.lower() in {"", "none", "unknown", "nouveau"}:
            return True
        module_names = {module.lower() for module in facts.driver_modules}
        return bool(module_names and "nvidia" not in module_names)

    @staticmethod
    def _has_nvidia_gpu(facts: SetupFacts) -> bool:
        return "nvidia" in {vendor.lower() for vendor in facts.gpu_vendors}

    @staticmethod
    def _is_ubuntu_like(facts: SetupFacts) -> bool:
        os_id = (facts.os_id or "").lower()
        os_like = (facts.os_like or "").lower()
        return os_id == "ubuntu" or "ubuntu" in os_like

    @staticmethod
    def _nvidia_recovery_family_label(facts: SetupFacts) -> str:
        if facts.package_manager == "apt" and SetupOrchestrator._is_ubuntu_like(facts):
            return "Ubuntu"
        labels = {
            "pacman": "Arch",
            "apt": "Debian",
            "dnf": "Fedora/RPM Fusion",
            "zypper": "openSUSE",
            "xbps": "Void",
            "apk": "Alpine",
            "emerge": "Gentoo",
        }
        return labels.get(facts.package_manager, "distro-specific")

    def _nvidia_recovery_group(self, facts: SetupFacts) -> CommandGroup | None:
        if facts.package_manager == "pacman":
            return self._arch_nvidia_recovery_group()
        if facts.package_manager == "apt":
            return (
                self._ubuntu_nvidia_recovery_group()
                if self._is_ubuntu_like(facts)
                else self._debian_nvidia_recovery_group()
            )
        if facts.package_manager == "dnf":
            return self._fedora_nvidia_recovery_group()
        if facts.package_manager == "zypper":
            if nvidia_open_kernel_supported(facts):
                return self._opensuse_nvidia_recovery_group()
            return None
        if facts.package_manager == "xbps":
            if nvidia_open_kernel_supported(facts):
                return self._void_nvidia_recovery_group()
            return None
        return None

    def _arch_nvidia_recovery_group(self) -> CommandGroup:
        commands = [
            self._arch_nvidia_install_command(),
            *_ARCH_NVIDIA_BOOT_CONFIG_COMMANDS,
            *_ARCH_NVIDIA_MODULE_LOAD_COMMANDS,
        ]
        return CommandGroup(
            id="quickhatch-arch-nvidia-recovery",
            stage=SetupStage.POSTBOOT_DRIVER,
            title="Arch NVIDIA Hardware Recovery",
            commands=commands,
            verification=["nvidia-smi"],
            rationale=(
                "QuickHatch detected NVIDIA hardware with an unhealthy driver state after "
                "install/reconnect, so it installs the Arch kernel module stack and verifies "
                "nvidia-smi before desktop work."
            ),
            source="repo",
            packages=list(self._arch_nvidia_packages()),
        )

    def _ubuntu_nvidia_recovery_group(self) -> CommandGroup:
        return CommandGroup(
            id="quickhatch-ubuntu-nvidia-recovery",
            stage=SetupStage.POSTBOOT_DRIVER,
            title="Ubuntu NVIDIA Hardware Recovery",
            commands=list(_UBUNTU_NVIDIA_COMMANDS),
            verification=["nvidia-smi"],
            rationale=(
                "QuickHatch detected NVIDIA hardware with an unhealthy driver state, so "
                "it uses Ubuntu's ubuntu-drivers tool to install the current recommended "
                "signed desktop driver and verifies nvidia-smi before desktop work."
            ),
            source="repo",
            packages=list(_UBUNTU_NVIDIA_PACKAGES),
        )

    def _debian_nvidia_recovery_group(self) -> CommandGroup:
        return CommandGroup(
            id="quickhatch-debian-nvidia-recovery",
            stage=SetupStage.POSTBOOT_DRIVER,
            title="Debian NVIDIA Hardware Recovery",
            commands=list(_DEBIAN_NVIDIA_COMMANDS),
            verification=["nvidia-smi"],
            rationale=(
                "QuickHatch detected NVIDIA hardware with an unhealthy driver state, so "
                "it enables Debian non-free firmware/components, installs the packaged "
                "NVIDIA driver path, and verifies nvidia-smi."
            ),
            source="repo",
            packages=list(_DEBIAN_NVIDIA_PACKAGES),
        )

    def _fedora_nvidia_recovery_group(self) -> CommandGroup:
        return CommandGroup(
            id="quickhatch-fedora-nvidia-recovery",
            stage=SetupStage.POSTBOOT_DRIVER,
            title="Fedora NVIDIA Hardware Recovery",
            commands=list(_FEDORA_NVIDIA_COMMANDS),
            verification=["nvidia-smi"],
            rationale=(
                "QuickHatch detected NVIDIA hardware with an unhealthy driver state, so "
                "it enables RPM Fusion's NVIDIA repository path, installs akmod-nvidia, "
                "and verifies nvidia-smi after module rebuild/reboot."
            ),
            source="community",
            packages=list(_FEDORA_NVIDIA_PACKAGES),
        )

    def _opensuse_nvidia_recovery_group(self) -> CommandGroup:
        return CommandGroup(
            id="quickhatch-opensuse-nvidia-recovery",
            stage=SetupStage.POSTBOOT_DRIVER,
            title="openSUSE NVIDIA Hardware Recovery",
            commands=list(_OPENSUSE_NVIDIA_COMMANDS),
            verification=["nvidia-smi"],
            rationale=(
                "QuickHatch detected NVIDIA hardware with an unhealthy driver state, so "
                "it enables the matching openSUSE NVIDIA repository, installs the G06 "
                "open kernel/userspace meta packages, and verifies nvidia-smi."
            ),
            source="repo",
            packages=list(_OPENSUSE_NVIDIA_PACKAGES),
        )

    def _void_nvidia_recovery_group(self) -> CommandGroup:
        return CommandGroup(
            id="quickhatch-void-nvidia-recovery",
            stage=SetupStage.POSTBOOT_DRIVER,
            title="Void NVIDIA Hardware Recovery",
            commands=list(_VOID_NVIDIA_COMMANDS),
            verification=["nvidia-smi"],
            rationale=(
                "QuickHatch detected NVIDIA hardware with an unhealthy driver state, so "
                "it enables Void's nonfree repository, installs the current NVIDIA package "
                "path for Turing-or-newer hardware, and verifies nvidia-smi."
            ),
            source="repo",
            packages=list(_VOID_NVIDIA_PACKAGES),
        )

    def _mark_nvidia_driver_manual_followup(self, facts: SetupFacts) -> None:
        plan = self.ctx.plan
        if plan is None:
            return
        label = self._nvidia_recovery_family_label(facts)
        reason = (
            f"NVIDIA hardware is present but QuickHatch does not have a safe automatic "
            f"driver recovery path for {label} with this GPU/package-manager state. "
            "Leaving driver setup as manual follow-up instead of guessing."
        )
        plan.driver_strategy.strategy = "nvidia_manual_followup"
        plan.driver_strategy.confidence = "manual_followup_required"
        plan.driver_strategy.source = "manual_followup"
        plan.driver_strategy.packages = []
        plan.driver_strategy.notes = reason
        stage_state = self.ctx.stage_state.setdefault(SetupStage.POSTBOOT_DRIVER.value, {})
        stage_state.setdefault("manual_followups", []).append(reason)
        self.bridge.add_message("agent", f"⚠️ {reason}")
        self.runlog.record(
            "nvidia_recovery_manual_followup",
            stage=SetupStage.POSTBOOT_DRIVER.value,
            facts=asdict(facts),
            reason=reason,
        )

    def _needs_arch_aur_helper_bootstrap(self, commands: list[str]) -> bool:
        facts = self.ctx.facts or SetupFacts()
        if facts.package_manager != "pacman" or facts.community_helper in {"yay", "paru"}:
            return False
        return any(self._uses_arch_community_helper_command(command) for command in commands)

    def _drop_model_arch_aur_helper_bootstrap_commands(
        self, stage: SetupStage, group: CommandGroup, commands: list[str]
    ) -> list[str]:
        facts = self.ctx.facts or SetupFacts()
        if facts.package_manager != "pacman":
            return commands
        kept: list[str] = []
        dropped: list[str] = []
        for command in commands:
            if self._is_model_arch_aur_helper_bootstrap_command(command):
                dropped.append(command)
                continue
            kept.append(command)
        if dropped:
            self.bridge.add_message(
                "agent",
                "[harness] Skipping model-authored AUR helper bootstrap; QuickHatch "
                "will use its conflict-safe helper setup when an AUR command needs one.",
            )
            self.runlog.record(
                "arch_aur_helper_bootstrap_dropped",
                stage=stage.value,
                group_id=group.id,
                commands=dropped,
            )
        return kept

    def _arch_yay_bootstrap_command(self) -> str:
        facts = self.ctx.facts or SetupFacts()
        return _ARCH_ROOT_YAY_BOOTSTRAP_COMMAND if facts.is_root else _ARCH_YAY_BOOTSTRAP_COMMAND

    @staticmethod
    def _is_arch_yay_bootstrap_command(command: str) -> bool:
        lower = command.lower()
        return "quickhatch-yay-bin" in lower and "yay-bin.git" in lower and "makepkg" in lower

    @staticmethod
    def _is_model_arch_aur_helper_bootstrap_command(command: str) -> bool:
        lower = (command or "").lower()
        if SetupOrchestrator._is_arch_yay_bootstrap_command(command):
            return False
        return bool(_ARCH_AUR_HELPER_BOOTSTRAP_RE.search(lower) and "makepkg" in lower)

    def _arch_aur_helper_available(self) -> bool:
        facts = self.ctx.facts or SetupFacts()
        return facts.package_manager == "pacman" and facts.community_helper in {"yay", "paru"}

    def _rewrite_arch_community_helper_command(
        self,
        command: str,
        *,
        stage: SetupStage,
        group: CommandGroup,
    ) -> str:
        facts = self.ctx.facts or SetupFacts()
        helper = facts.community_helper
        if facts.package_manager != "pacman" or helper not in {"yay", "paru"}:
            return command
        helpers = arch_community_helpers_in_command(command)
        if not helpers:
            return command
        if helpers == {helper}:
            return (
                self._wrap_root_arch_community_helper_command(command) if facts.is_root else command
            )
        rewritten = command
        for other in {"yay", "paru"} - {helper}:
            rewritten = re.sub(rf"(?<![\w/-]){other}(?=\s)", helper, rewritten)
            rewritten = re.sub(
                rf"(?<![\w-])/usr/bin/{other}(?=\s)", f"/usr/bin/{helper}", rewritten
            )
        if rewritten != command:
            self.bridge.add_message(
                "agent",
                f"[harness] Rewriting AUR helper command to use detected `{helper}`.",
            )
            self.runlog.record(
                "arch_aur_helper_command_rewritten",
                stage=stage.value,
                group_id=group.id,
                original=command,
                rewritten=rewritten,
                helper=helper,
            )
        if facts.is_root and self._uses_arch_community_helper_command(rewritten):
            rewritten = self._wrap_root_arch_community_helper_command(rewritten)
        return rewritten

    def _wrap_root_arch_community_helper_command(self, command: str) -> str:
        if self._has_explicit_non_root_user_wrapper(command):
            return command
        wrapped = f"sudo -u {_ARCH_AUR_BUILD_USER} bash -lc {shlex.quote(command)}"
        self.bridge.add_message(
            "agent",
            f"[harness] Running AUR helper as `{_ARCH_AUR_BUILD_USER}` because makepkg/yay must not run as root.",
        )
        self.runlog.record(
            "arch_aur_helper_wrapped_root_command",
            original=command,
            rewritten=wrapped,
            build_user=_ARCH_AUR_BUILD_USER,
        )
        return wrapped

    @staticmethod
    def _has_explicit_non_root_user_wrapper(command: str) -> bool:
        return bool(
            re.search(r"\b(?:sudo|runuser)\s+(?:-[^\s]+\s+)*-u\s+(?!root\b)\S+", command)
            or re.search(r"\bsu\s+-?\s*(?!root\b)[A-Za-z_][A-Za-z0-9_-]*\b.*\s-c\b", command)
        )

    def _try_bootstrap_arch_aur_helper_for_command(
        self,
        command: str,
        *,
        stage: SetupStage,
        group: CommandGroup,
        stage_state: dict[str, Any],
    ) -> GroupFailure | None:
        facts = self.ctx.facts or SetupFacts()
        if (
            facts.package_manager != "pacman"
            or facts.community_helper in {"yay", "paru"}
            or not self._uses_arch_community_helper_command(command)
        ):
            return None
        if stage_state.get("arch_aur_helper_bootstrap_attempted"):
            return None
        stage_state["arch_aur_helper_bootstrap_attempted"] = True
        bootstrap_command = self._arch_yay_bootstrap_command()
        blocking_decisions = [
            decision
            for decision in preflight_setup_decisions(
                bootstrap_command,
                facts,
                shadow_mode=os.environ.get("QUICKHATCH_PREFLIGHT_SHADOW") == "1",
            )
            if decision.kind == PreflightDecisionKind.BLOCK
        ]
        if blocking_decisions:
            detail = blocking_decisions[0].reason
            self.runlog.record(
                "arch_aur_helper_bootstrap_preflight_blocked",
                stage=stage.value,
                group_id=group.id,
                command=bootstrap_command,
                decision=_preflight_decision_payload(blocking_decisions[0]),
                blocked_command=command,
            )
            return GroupFailure(
                failure_type="preflight",
                command=bootstrap_command,
                stdout="",
                stderr="",
                detail=detail,
            )
        self.bridge.add_message(
            "agent",
            "AUR helper is missing; installing `yay` before retrying the planned community command.",
        )
        self.bridge.add_message("agent", f"`{redact_sensitive_text(bootstrap_command)}`")
        self.runlog.record(
            "arch_aur_helper_bootstrap_started",
            stage=stage.value,
            group_id=group.id,
            command=bootstrap_command,
            blocked_command=command,
        )
        rc, stdout, stderr = self.runtime.ssh_exec(
            bootstrap_command,
            self._command_timeout(bootstrap_command),
        )
        self.ctx.results_log.append(
            redact_sensitive_text(f"$ {bootstrap_command}\n[rc={rc}]\n{stdout}\n{stderr}".strip())
        )
        self.ctx.results_log[:] = self.ctx.results_log[-self.runtime.results_log_limit :]
        self.runlog.record(
            "arch_aur_helper_bootstrap_result",
            stage=stage.value,
            group_id=group.id,
            rc=rc,
            stdout_tail=(stdout or "")[-800:],
            stderr_tail=(stderr or "")[-800:],
        )
        if rc != 0:
            detail = stderr.strip() or stdout.strip() or "failed to install yay"
            self.bridge.add_message(
                "agent",
                self._format_group_failure_message(
                    group.title,
                    "AUR helper bootstrap failed",
                    bootstrap_command,
                    rc,
                    stdout,
                    stderr,
                ),
            )
            return GroupFailure(
                failure_type="command",
                command=bootstrap_command,
                stdout=stdout,
                stderr=stderr,
                detail=detail,
            )
        self._update_from_command(bootstrap_command, stdout, stderr, stage=stage)
        facts = self._refresh_facts(stage=stage)
        if facts.package_manager == "pacman" and facts.community_helper not in {"yay", "paru"}:
            facts.community_helper = "yay"
        self.bridge.add_message("agent", "✓ AUR helper installed — retrying planned command.")
        return None

    @staticmethod
    def _uses_arch_community_helper_command(command: str) -> bool:
        return bool(arch_community_helpers_in_command(command))

    def _arch_nvidia_install_command(self) -> str:
        packages = " ".join(self._arch_nvidia_packages())
        return (
            f"{_ARCH_ENABLE_MULTILIB_COMMAND} && sudo pacman -Syu --needed --noconfirm {packages}"
        )

    def _arch_nvidia_packages(self) -> tuple[str, ...]:
        facts = self.ctx.facts or SetupFacts()
        kernel_release = (facts.kernel_release or "").lower()
        if "-lts" in kernel_release:
            return _ARCH_NVIDIA_LTS_OPEN_PACKAGES
        if "-zen" in kernel_release:
            return _ARCH_NVIDIA_ZEN_OPEN_PACKAGES
        if "-hardened" in kernel_release:
            return _ARCH_NVIDIA_HARDENED_OPEN_PACKAGES
        return _ARCH_NVIDIA_OPEN_PACKAGES

    def _arch_nvidia_requires_manual_package_resolution(self) -> bool:
        facts = self.ctx.facts or SetupFacts()
        return bool(facts.gpu_inventory and not nvidia_open_kernel_supported(facts))

    def _maybe_reboot_for_nvidia_driver(
        self,
        stage: SetupStage,
        group: CommandGroup,
        stage_state: dict[str, Any],
    ) -> None:
        commands = [*group.commands, *group.verification]
        if not self._is_nvidia_driver_group(stage, group, commands):
            return
        if stage_state.get("nvidia_reboot_attempted"):
            return
        facts = self._refresh_facts(stage=stage)
        if facts.nvidia_smi_state == "ok" and not facts.nvidia_nouveau_bound:
            return
        if not facts.nvidia_nouveau_bound and facts.nvidia_smi_state not in {
            "present_but_failing",
            "missing",
        }:
            return
        stage_state["nvidia_reboot_attempted"] = True
        command = (
            "if command -v systemctl >/dev/null 2>&1; then "
            "sudo systemctl enable sshd NetworkManager 2>/dev/null || "
            "sudo systemctl enable ssh NetworkManager 2>/dev/null || true; "
            "fi; sudo reboot"
        )
        self.bridge.add_message(
            "agent",
            "NVIDIA driver changes need a controlled reboot before verification.",
        )
        self.bridge.add_message("agent", f"`{redact_sensitive_text(command)}`")
        self.runlog.record(
            "nvidia_driver_reboot",
            stage=stage.value,
            group_id=group.id,
            facts=asdict(facts),
            command=command,
        )
        rc, stdout, stderr = self.runtime.ssh_exec(command, 60)
        self.ctx.results_log.append(
            redact_sensitive_text(f"$ {command}\n[rc={rc}]\n{stdout}\n{stderr}".strip())
        )
        self.ctx.results_log[:] = self.ctx.results_log[-self.runtime.results_log_limit :]
        if rc != 0:
            raise RuntimeError(
                "Failed to reboot after NVIDIA driver configuration: "
                f"{(stderr or stdout or '').strip()[:240]}"
            )
        reconnected, fresh_info, new_user = self.runtime.wait_for_reconnect(self.runtime.user)
        self.runlog.record(
            "nvidia_driver_reconnect",
            stage=stage.value,
            group_id=group.id,
            reconnected=reconnected,
            fresh_info=fresh_info,
            new_user=new_user,
        )
        if not reconnected:
            raise RuntimeError(fresh_info or "Target did not reconnect after NVIDIA driver reboot.")
        if new_user:
            self.runtime.user = new_user
        self.ctx.system_info = fresh_info or self.ctx.system_info
        self._refresh_facts(stage=stage)

    def _needs_live_disk_prep(self, stage: SetupStage, commands: list[str]) -> bool:
        facts = self.ctx.facts or SetupFacts()
        if stage != SetupStage.INSTALL_OR_SKIP or not facts.is_live:
            return False
        return any(
            pattern.search(command)
            for command in commands
            for pattern in _LIVE_DISK_DESTRUCTIVE_PATTERNS
        )

    @staticmethod
    def _live_disk_prep_commands(commands: list[str]) -> list[str]:
        prep = list(_LIVE_DISK_PREP_BASE_COMMANDS)
        for disk in SetupOrchestrator._extract_destructive_target_disks(commands):
            disk_q = shlex.quote(disk)
            prep.append(
                f"for p in $(lsblk -lnpo NAME {disk_q} 2>/dev/null | tail -n +2 | tac); "
                'do sudo umount "$p" 2>/dev/null || true; done'
            )
        return prep

    @staticmethod
    def _extract_destructive_target_disks(commands: list[str]) -> list[str]:
        disks: list[str] = []
        seen: set[str] = set()
        bare_disk_re = re.compile(
            r"--(?:drives|boot-drive)=(?P<disk>nvme\d+n\d+|mmcblk\d+|sd[a-z]+|vd[a-z]+|xvd[a-z]+)\b"
        )
        for command in commands:
            if not any(pattern.search(command) for pattern in _LIVE_DISK_DESTRUCTIVE_PATTERNS):
                continue
            for disk in SetupOrchestrator._extract_linux_block_devices(command):
                if disk not in seen:
                    seen.add(disk)
                    disks.append(disk)
            for match in bare_disk_re.finditer(command):
                disk = f"/dev/{match.group('disk')}"
                if disk not in seen:
                    seen.add(disk)
                    disks.append(disk)
        return disks

    @staticmethod
    def _extract_operation_target_disks(operations: list[Any]) -> list[str]:
        disks: list[str] = []
        for operation in operations:
            if getattr(operation, "kind", "") not in {
                "partition_disk",
                "format_partition",
                "mount_partition",
                "install_bootloader",
            }:
                continue
            disk = SetupOrchestrator._target_to_linux_disk(getattr(operation, "target", ""))
            if disk:
                disks.append(disk)
        return SetupOrchestrator._unique_disks(disks)

    @staticmethod
    def _extract_linux_block_devices(text: str) -> list[str]:
        device_re = re.compile(
            r"(/dev/(?:nvme\d+n\d+|mmcblk\d+|sd[a-z]+|vd[a-z]+|xvd[a-z]+))(?:p?\d+)?\b"
        )
        return [match.group(1) for match in device_re.finditer(text or "")]

    @staticmethod
    def _target_to_linux_disk(target: str) -> str:
        disks = SetupOrchestrator._extract_linux_block_devices(str(target or ""))
        return disks[0] if disks else ""

    @staticmethod
    def _unique_disks(disks: list[str]) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for disk in disks:
            if disk and disk not in seen:
                seen.add(disk)
                unique.append(disk)
        return unique

    @staticmethod
    def _first_live_disk_destructive_index(commands: list[str]) -> int:
        for index, command in enumerate(commands):
            if any(pattern.search(command) for pattern in _LIVE_DISK_DESTRUCTIVE_PATTERNS):
                return index
        return len(commands)

    @staticmethod
    def _format_group_failure_message(
        title: str,
        label: str,
        command: str,
        rc: int,
        stdout: str,
        stderr: str,
    ) -> str:
        tail = (stderr or stdout or "").strip()
        if len(tail) > 500:
            tail = tail[-500:]
        if not tail:
            tail = "No output."
        return (
            f"⚠️ {label} in '{title}' (rc={rc}): "
            f"`{redact_sensitive_text(command)}`\n{redact_sensitive_text(tail)}"
        )

    def _repair_model_call(self, messages: list[dict[str, str]]) -> str:
        repair_config = copy.copy(self.runtime.config)
        repair_config.native_search = self.runtime.native_search_active
        return self._call_model_with_retries(repair_config, messages, context="repair")

    def _call_model_with_retries(
        self,
        model_config: AIConfig,
        messages: list[dict[str, str]],
        *,
        context: str,
    ) -> str:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            started = time.monotonic()
            try:
                return self._call_runtime_model_text(model_config, messages)
            except Exception as caught:
                error = caught
            elapsed = max(0.0, time.monotonic() - started)
            last_error = error
            self.runlog.record(
                "model_call_failed",
                context=context,
                attempt=attempt,
                elapsed_seconds=round(elapsed, 1),
                timeout_seconds=_SETUP_MODEL_CALL_TIMEOUT_SECONDS,
                error=str(error),
            )
            if attempt >= 3:
                break
            delay = min(3 * attempt, 12)
            self.bridge.add_message(
                "agent",
                f"⚠️ {context.capitalize()} LLM call failed ({attempt}/3; "
                f"attempt took {elapsed:.0f}s, timeout {_SETUP_MODEL_CALL_TIMEOUT_SECONDS}s): "
                f"{str(error)[:220]}. Retrying in {delay}s…",
            )
            time.sleep(delay)
        raise RuntimeError(f"{context.capitalize()} LLM call failed after 3 attempts: {last_error}")

    def _call_runtime_model_text(
        self, model_config: AIConfig, messages: list[dict[str, str]]
    ) -> str:
        try:
            return self.runtime.call_model_text(
                model_config,
                messages,
                _SETUP_MODEL_CALL_TIMEOUT_SECONDS,
            )
        except TypeError as exc:
            if not self._looks_like_timeout_callback_mismatch(exc):
                raise
            return self.runtime.call_model_text(model_config, messages)

    @staticmethod
    def _looks_like_timeout_callback_mismatch(exc: TypeError) -> bool:
        message = str(exc)
        return (
            "positional" in message
            or "required positional" in message
            or "unexpected keyword" in message
            or "takes" in message
            or "argument" in message
        )

    def _repair_search_result(self, action: dict[str, Any]) -> str:
        query = str(action.get("query", "")).strip()
        name = str(action.get("action", ""))
        try:
            if name == "repo_search":
                result = self.runtime.repo_search(query)
            elif name == "community_search":
                result = self.runtime.community_search(query)
            elif name == "web_search":
                result = self.runtime.web_search(
                    query,
                    self._bounded_lookup_max_results(action.get("max_results", 5)),
                )
            else:
                raise RuntimeError(f"Unsupported repair search action: {name}")
        except Exception as exc:
            error_text = redact_sensitive_text(str(exc))
            self.runlog.record(
                "repair_search_failed",
                action=name,
                query=query,
                error=error_text,
            )
            return (
                f"[{name} failed]\n"
                f"query={query}\n"
                f"error={error_text}\n"
                "Search failed. If this item remains unresolved, return manual_followup "
                "with the exact app/item and reason instead of retrying stale package names."
            )
        return f"[{name}]\nquery={query}\n{result}"

    def _repair_failed_group(
        self,
        stage: SetupStage,
        failed_group: CommandGroup,
        failure: GroupFailure,
        *,
        repair_attempt: int,
    ) -> dict[str, Any]:
        prompt = build_repair_prompt(
            stage=stage,
            failed_group=failed_group,
            failure_type=failure.failure_type,
            failed_command=redact_sensitive_text(failure.command),
            stdout_tail=redact_sensitive_text(failure.stdout),
            stderr_tail=redact_sensitive_text(failure.stderr),
            facts=self.ctx.facts or SetupFacts(),
            recent_results=self.ctx.results_log,
            native_search_active=self.runtime.native_search_active,
        )
        self.runlog.record(
            "repair_prompt",
            stage=stage.value,
            group_id=failed_group.id,
            repair_attempt=repair_attempt,
            prompt=prompt,
        )
        messages = [{"role": "user", "content": prompt}]
        search_performed = False
        for turn in range(1, self.MAX_REPAIR_TURNS + 1):
            try:
                response_text = self._repair_model_call(messages)
            except RuntimeError as exc:
                if stage in {SetupStage.POSTBOOT_DRIVER, SetupStage.DESKTOP, SetupStage.APPS}:
                    reason = (
                        f"Repair LLM unavailable for '{failed_group.title}': {exc}. "
                        "Leaving this non-install group as manual follow-up instead of "
                        "blocking setup."
                    )
                    self.runlog.record(
                        "repair_model_unavailable_manual_followup",
                        stage=stage.value,
                        group_id=failed_group.id,
                        repair_attempt=repair_attempt,
                        repair_turn=turn,
                        error=str(exc),
                        reason=reason,
                    )
                    return {"action": "manual_followup", "reason": reason}
                raise
            self.runlog.record(
                "repair_response",
                stage=stage.value,
                group_id=failed_group.id,
                repair_attempt=repair_attempt,
                repair_turn=turn,
                text=response_text,
            )
            try:
                action = extract_repair_action(
                    response_text,
                    fallback_stage=stage,
                    failed_group=failed_group,
                )
            except RepairActionValidationError as exc:
                self.runlog.record(
                    "repair_response_invalid",
                    stage=stage.value,
                    group_id=failed_group.id,
                    repair_attempt=repair_attempt,
                    repair_turn=turn,
                    error=str(exc),
                )
                messages = [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": response_text},
                    {
                        "role": "user",
                        "content": (
                            "[quickhatch repair parser]\n"
                            f"Your previous repair response was invalid JSON: {exc}\n"
                            "Return exactly one complete JSON object. Do not use Markdown fences, "
                            "comments, prose, or truncated strings. Use action retry_group, "
                            "repo_search, community_search, web_search, question, or manual_followup."
                        ),
                    },
                ]
                continue
            self.runlog.record(
                "repair_action",
                stage=stage.value,
                group_id=failed_group.id,
                repair_attempt=repair_attempt,
                repair_turn=turn,
                action=action["action"],
            )
            if action["action"] in {"repo_search", "community_search", "web_search"}:
                search_performed = True
                result = self._repair_search_result(action)
                messages = [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": response_text},
                    {"role": "user", "content": result},
                ]
                continue
            if action["action"] == "question":
                answer = self._ask_blocking_question(
                    action["question"],
                    choices=action.get("choices"),
                    stage=stage,
                    context={
                        "repair": True,
                        "group_id": failed_group.id,
                        "failure_type": failure.failure_type,
                    },
                    timeout=float(action.get("timeout_seconds", 600) or 600),
                )
                if not answer:
                    if not _coerce_model_bool(
                        action.get("required", True), default=True
                    ) or stage in {
                        SetupStage.DESKTOP,
                        SetupStage.APPS,
                    }:
                        return {
                            "action": "manual_followup",
                            "reason": (
                                "Repair question timed out; this non-install item was left "
                                "for manual follow-up instead of stopping setup."
                            ),
                        }
                    raise RuntimeError(
                        f"Repair question timed out for required item in '{failed_group.title}'."
                    )
                answer_text = f"[question answer]\n{answer or 'no answer'}"
                messages = [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": response_text},
                    {"role": "user", "content": answer_text},
                ]
                continue
            if (
                action["action"] == "retry_group"
                and self._repair_requires_research(stage, failed_group, failure)
                and not search_performed
                and not action["group"].evidence
            ):
                self.runlog.record(
                    "repair_retry_rejected_missing_evidence",
                    stage=stage.value,
                    group_id=failed_group.id,
                    repair_attempt=repair_attempt,
                    repair_turn=turn,
                )
                messages = [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": response_text},
                    {
                        "role": "user",
                        "content": (
                            "[quickhatch repair policy]\n"
                            "Do not retry this package/driver/desktop/app failure without fresh evidence. "
                            "First return repo_search, community_search, or web_search; or return retry_group "
                            "with group.evidence explaining the current source-backed basis for each changed command."
                        ),
                    },
                ]
                continue
            if action["action"] == "retry_group":
                repeated_reason = self._repair_same_missing_target_retry_reason(
                    stage,
                    failed_group,
                    failure,
                    action["group"],
                    search_performed=search_performed,
                )
                if repeated_reason:
                    self.runlog.record(
                        "repair_retry_rejected_same_strategy",
                        stage=stage.value,
                        group_id=failed_group.id,
                        repair_attempt=repair_attempt,
                        repair_turn=turn,
                        reason=repeated_reason,
                    )
                    messages = [
                        {"role": "user", "content": prompt},
                        {"role": "assistant", "content": response_text},
                        {
                            "role": "user",
                            "content": (
                                "[quickhatch repair policy]\n"
                                f"{repeated_reason}\n"
                                "Return a meaningfully different safe repair: search first, "
                                "switch source explicitly, add a real local-state diagnostic "
                                "when the failure is transient, or mark only the unresolved "
                                "item manual_followup."
                            ),
                        },
                    ]
                    continue
            return action
        if stage in {SetupStage.POSTBOOT_DRIVER, SetupStage.DESKTOP, SetupStage.APPS}:
            return {
                "action": "manual_followup",
                "reason": (
                    f"Repair loop exhausted for '{failed_group.title}' after "
                    f"{self.MAX_REPAIR_TURNS} invalid or non-actionable repair turn(s)."
                ),
            }
        raise RuntimeError(f"Repair loop exhausted for '{failed_group.title}'.")

    @staticmethod
    def _repair_requires_research(
        stage: SetupStage, failed_group: CommandGroup, failure: GroupFailure
    ) -> bool:
        if stage == SetupStage.DESKTOP and failure.failure_type == "desktop_design_feedback":
            return False
        text = " ".join(
            [
                stage.value,
                failed_group.title,
                failed_group.source,
                failure.command,
                failure.detail,
                " ".join(failed_group.packages),
            ]
        ).lower()
        if stage in {SetupStage.POSTBOOT_DRIVER, SetupStage.DESKTOP, SetupStage.APPS}:
            return True
        return any(
            marker in text
            for marker in (
                "pacman",
                "apt",
                "dnf",
                "zypper",
                "apk",
                "xbps",
                "emerge",
                "package",
                "driver",
                "nvidia",
                "theme",
                "desktop",
                "aur",
                "yay",
                "paru",
            )
        )

    @staticmethod
    def _repair_same_missing_target_retry_reason(
        stage: SetupStage,
        failed_group: CommandGroup,
        failure: GroupFailure,
        replacement_group: CommandGroup,
        *,
        search_performed: bool,
    ) -> str:
        if search_performed or not SetupOrchestrator._repair_requires_research(
            stage, failed_group, failure
        ):
            return ""
        original = build_attempt_signature(
            stage=stage.value,
            group_id=failed_group.id,
            group_title=failed_group.title,
            command=failure.command,
            stdout=failure.stdout,
            stderr=failure.stderr,
            detail=failure.detail,
        )
        if original.failure_category != "package_missing":
            return ""
        repeated_commands: list[str] = []
        for command in replacement_group.commands:
            if is_diagnostic_command(command):
                continue
            current = build_attempt_signature(
                stage=stage.value,
                group_id=replacement_group.id,
                group_title=replacement_group.title,
                command=command,
                stdout=failure.stdout,
                stderr=failure.stderr,
                detail=failure.detail,
            )
            if current.loop_key == original.loop_key:
                repeated_commands.append(command)
        if not repeated_commands:
            return ""
        packages = ", ".join(original.packages) or failure.command
        return (
            "Repair retry repeats the same missing-target strategy for "
            f"{packages} before any repair search or useful diagnosis."
        )

    def _mark_optional_apps_manual_followup(self, group: CommandGroup) -> None:
        if not self.ctx.plan:
            return
        group_pkgs = {pkg.lower() for pkg in group.packages}
        for app in self.ctx.plan.apps:
            if app.optional_slow and (
                app.package_name.lower() in group_pkgs or app.name.lower() in group.title.lower()
            ):
                app.status = PlanItemStatus.MANUAL_FOLLOWUP
                app.reason = "User skipped optional slow install during setup."

    def _mark_unanswered_optional_slow_apps_manual_followup(self) -> None:
        if not self.ctx.plan:
            return
        names: list[str] = []
        for app in self.ctx.plan.apps:
            if app.status != PlanItemStatus.OPTIONAL_SLOW_PENDING_CONFIRMATION:
                continue
            app.status = PlanItemStatus.MANUAL_FOLLOWUP
            app.reason = (
                app.reason
                or "Optional slow app was not explicitly confirmed during setup; left for manual follow-up."
            )
            names.append(app.name)
        if not names:
            return
        self._record_optional_slow_apps_manual_followup(names)

    def _record_optional_slow_apps_manual_followup(self, names: list[str]) -> None:
        if not names:
            return
        reason = "Optional slow app(s) left for manual follow-up: " + ", ".join(sorted(names))
        self.ctx.stage_state.setdefault(SetupStage.APPS.value, {}).setdefault(
            "manual_followups", []
        ).append({"reason": reason, "apps": sorted(names)})
        self.runlog.record(
            "optional_slow_manual_followup",
            stage=SetupStage.APPS.value,
            apps=sorted(names),
            reason=reason,
        )
        self.bridge.add_message("agent", reason)

    def _mark_group_manual_followup(
        self,
        group: CommandGroup,
        *,
        stage: SetupStage,
        reason: str,
        app_names: list[str],
    ) -> None:
        if not self.ctx.plan:
            return
        self.ctx.stage_state.setdefault(stage.value, {})["failed_verifications"] = []
        if stage != SetupStage.APPS:
            if stage == SetupStage.POSTBOOT_DRIVER and self.ctx.facts:
                if "nvidia" in self.ctx.facts.gpu_vendors:
                    self.ctx.plan.driver_strategy.strategy = "nvidia_manual_followup"
                    self.ctx.plan.driver_strategy.confidence = "manual_followup_required"
                    self.ctx.plan.driver_strategy.source = "manual_followup"
                    self.ctx.plan.driver_strategy.notes = reason
                    self.ctx.plan.driver_strategy.packages = []
            self.ctx.stage_state.setdefault(stage.value, {}).setdefault(
                "manual_followups", []
            ).append({"group_id": group.id, "reason": reason})
            return
        self._mark_apps_installed_after_partial_group(group)
        app_names_lower = {name.lower() for name in app_names if name}
        group_pkgs = {pkg.lower() for pkg in group.packages}
        title = group.title.lower()
        command_text = " ".join([group.title, *group.commands, *group.verification])
        command_tokens = set(self._app_catalog_key(command_text).split())
        matched = False
        for app in self.ctx.plan.apps:
            if app.status in {
                PlanItemStatus.INSTALLED,
                PlanItemStatus.COMPLETED,
                PlanItemStatus.EXPLICITLY_SKIPPED,
            }:
                continue
            if app.package_name.lower() in group_pkgs:
                app.status = PlanItemStatus.MANUAL_FOLLOWUP
                app.reason = reason
                matched = True
                continue
            if app.name.lower() in app_names_lower or app.name.lower() in title:
                app.status = PlanItemStatus.MANUAL_FOLLOWUP
                app.reason = reason
                matched = True
                continue
            variants = self._package_name_variants(app.name, app.package_name)
            if any(
                self._variant_appears_in_command_text(variant, command_text) for variant in variants
            ):
                app.status = PlanItemStatus.MANUAL_FOLLOWUP
                app.reason = reason
                matched = True
                continue
            meaningful_tokens = (
                self._meaningful_app_name_tokens(app.name) - _WEAK_APP_BINARY_DESKTOP_ALIASES
            )
            if meaningful_tokens and meaningful_tokens.intersection(command_tokens):
                app.status = PlanItemStatus.MANUAL_FOLLOWUP
                app.reason = reason
                matched = True
        if not matched:
            self.ctx.stage_state.setdefault(stage.value, {}).setdefault(
                "manual_followups", []
            ).append({"group_id": group.id, "reason": reason})

    def _mark_apps_installed_after_partial_group(self, group: CommandGroup) -> None:
        if self.ctx.plan is None or self.ctx.facts is None:
            return
        planned_packages = self._app_group_planned_packages(group)
        if not planned_packages:
            return
        query_packages = self._app_group_package_query_candidates(group)
        installed = self._query_installed_native_packages(query_packages, self.ctx.facts)
        if not installed:
            return
        installed_in_group = {
            package
            for package in planned_packages
            if self._planned_package_is_installed(package, installed, group)
        }
        if not installed_in_group:
            return
        self._mark_apps_installed_by_prefilter(installed_in_group, installed, group)
        self.runlog.record(
            "apps_marked_installed_after_partial_group",
            stage=SetupStage.APPS.value,
            group_id=group.id,
            installed=sorted(installed_in_group),
        )

    def _mark_group_completion(self, group: CommandGroup, *, stage: SetupStage) -> None:
        if not self.ctx.plan:
            return
        if stage != SetupStage.APPS:
            return
        group_pkgs = {pkg.lower() for pkg in group.packages}
        title = group.title.lower()
        command_text = " ".join([*group.commands, *group.verification, group.title]).lower()
        for app in self.ctx.plan.apps:
            variants = self._package_name_variants(app.name, app.package_name)
            if variants and variants.intersection(group_pkgs):
                app.status = PlanItemStatus.INSTALLED
                continue
            if any(
                self._variant_appears_in_command_text(variant, command_text) for variant in variants
            ):
                app.status = PlanItemStatus.INSTALLED
                continue
            if app.name.lower() in title and app.status == PlanItemStatus.PENDING:
                app.status = PlanItemStatus.INSTALLED

    def _update_from_command(
        self, command: str, stdout: str, stderr: str, *, stage: SetupStage
    ) -> None:
        del stderr
        if stage == SetupStage.INSTALL_OR_SKIP and re.search(r"\b(?:useradd|adduser)\b", command):
            created_user = self._extract_created_user(command)
            if created_user:
                self.ctx.created_user = created_user
        if self._is_arch_yay_bootstrap_command(command) and self.ctx.facts is not None:
            self.ctx.facts.community_helper = "yay"
        if stage == SetupStage.DESKTOP:
            state = self.ctx.stage_state.setdefault(stage.value, {})
            wrong_de_reason = self._wrong_desktop_environment_reason(command)
            if wrong_de_reason:
                self._record_desktop_design_manual_followup(
                    state,
                    reason=wrong_de_reason,
                    source="desktop_wrong_environment",
                    command=command,
                )
            if self._command_persists_desktop_config(command):
                if self._command_targets_wrong_desktop_user(command):
                    reason = self._wrong_desktop_user_reason(command)
                    self._record_desktop_design_manual_followup(
                        state,
                        reason=reason,
                        source="desktop_wrong_user",
                        command=command,
                    )
                    self.runlog.record(
                        "desktop_wrong_user_manual_followup",
                        stage=SetupStage.DESKTOP.value,
                        command=command,
                        reason=reason,
                    )
                else:
                    state["persisted_config_written"] = True
            if self._command_attempts_desktop_design(command):
                state["desktop_design_attempted"] = True
            if self._command_attempts_wallpaper(command):
                state["wallpaper_attempted"] = True
            self._record_desktop_design_expectations(command, state)
            if "plasma-apply" in command and stdout.strip() == "":
                if self.ctx.facts and self.ctx.facts.displays:
                    self.ctx.facts.desktop_runtime_state = DesktopRuntimeState.LIVE_APPLY_SUCCEEDED

    @staticmethod
    def _command_persists_desktop_config(command: str) -> bool:
        lowered = command.lower()
        return any(
            token in lowered
            for token in (
                "kwriteconfig",
                "gsettings",
                "xfconf-query",
                "systemctl enable gdm",
                "systemctl enable --now gdm",
                "systemctl enable sddm",
                "systemctl enable --now sddm",
                "systemctl enable lightdm",
                "systemctl enable --now lightdm",
                ".dmrc",
                "/.config/",
                "/etc/sddm.conf",
                "/etc/sddm.conf.d/",
                "/etc/lightdm/",
                "kscreen",
                "kdeglobals",
            )
        )

    @staticmethod
    def _desktop_command_can_persist_without_gui(command: str) -> bool:
        lowered = command.lower()
        if "gsettings" in lowered:
            return "org.gnome.desktop.interface color-scheme" in lowered
        return any(
            token in lowered
            for token in (
                "kwriteconfig",
                "xfconf-query",
                "dconf load",
                "systemctl enable gdm",
                "systemctl enable --now gdm",
                "systemctl enable sddm",
                "systemctl enable --now sddm",
                "systemctl enable lightdm",
                "systemctl enable --now lightdm",
                ".dmrc",
                "/.config/",
                "/etc/sddm.conf",
                "/etc/sddm.conf.d/",
                "/etc/lightdm/",
                "kdeglobals",
            )
        )

    def _command_targets_wrong_desktop_user(self, command: str) -> bool:
        lowered = command.lower()
        target_users = self._desktop_target_user_names()
        if not self._desktop_root_is_target_user() and (
            "/root/" in lowered
            or "home=/root" in lowered
            or "dbus_session_bus_address=" in lowered
            or re.search(r"\bsudo\s+(?:-[^\s]+\s+)*-u\s+root\b", lowered)
            or re.search(r"\bsudo\s+gsettings\b", lowered)
        ):
            return True
        home_users = {
            match.group(1).lower() for match in re.finditer(r"/home/([^/\s'\"]+)", command)
        }
        return bool(home_users and target_users and not home_users.issubset(target_users))

    def _desktop_target_user_names(self) -> set[str]:
        facts = self.ctx.facts or SetupFacts()
        users = {
            str(facts.raw.get("QH_GUI_USER", "") or "").strip(),
            str(facts.login_user or "").strip(),
            str(facts.current_user or "").strip(),
            str(self.runtime.user or "").strip(),
        }
        return {user.lower() for user in users if user}

    def _desktop_root_is_target_user(self) -> bool:
        target_users = self._desktop_target_user_names()
        return bool(target_users) and target_users == {"root"}

    def _wrong_desktop_user_reason(self, command: str) -> str:
        target = ", ".join(sorted(self._desktop_target_user_names())) or "target user"
        return (
            "Desktop config command targets root or a different home/user than "
            f"{target}; it was not counted as verified target-user desktop persistence: "
            f"{redact_sensitive_text(command)}"
        )

    def _wrong_desktop_environment_reason(self, command: str) -> str:
        command_family = self._desktop_command_family(command)
        if not command_family:
            return ""
        actual = self._current_desktop_family()
        if actual == command_family:
            return ""
        if actual in {"kde", "gnome", "xfce"}:
            return (
                f"Desktop command uses {command_family.upper()} tooling while the target "
                f"desktop facts report {actual.upper()}; it was not counted as verified "
                f"target desktop design: {redact_sensitive_text(command)}"
            )
        return (
            f"Desktop command uses {command_family.upper()}-specific tooling, but the "
            "target desktop environment is unknown; verify the session and apply the "
            f"matching desktop strategy before declaring success: {redact_sensitive_text(command)}"
        )

    def _current_desktop_family(self) -> str:
        facts = self.ctx.facts or SetupFacts()
        env = (facts.desktop_design.environment or "").strip().lower()
        if "plasma" in env or "kde" in env:
            return "kde"
        if "gnome" in env:
            return "gnome"
        if "xfce" in env:
            return "xfce"
        manager = (facts.display_manager or "").strip().lower()
        if manager == "sddm":
            return "kde"
        if manager == "gdm":
            return "gnome"
        if manager == "lightdm":
            return "xfce"
        return ""

    @staticmethod
    def _desktop_command_family(command: str) -> str:
        lowered = (command or "").lower()
        if any(
            token in lowered
            for token in (
                "plasma-apply-",
                "lookandfeeltool",
                "kwriteconfig",
                "kreadconfig",
                "kcminputrc",
                "kdeglobals",
                "kwinrc",
                "org.kde.",
                "qdbus org.kde",
                "qdbus6 org.kde",
            )
        ):
            return "kde"
        if "gsettings" in lowered and "org.gnome." in lowered:
            return "gnome"
        if "dconf" in lowered and "org/gnome" in lowered:
            return "gnome"
        if "xfconf-query" in lowered or "xfdesktop" in lowered:
            return "xfce"
        return ""

    @staticmethod
    def _record_desktop_design_manual_followup(
        state: dict[str, Any],
        *,
        reason: str,
        source: str,
        command: str = "",
    ) -> None:
        item: dict[str, Any] = {"source": source, "reason": reason}
        if command:
            item["command"] = command
        manual_followups = list(state.get("manual_followups") or [])
        if item not in manual_followups:
            manual_followups.append(item)
        state["manual_followups"] = manual_followups

    @staticmethod
    def _replace_desktop_design_feedback_followups(
        state: dict[str, Any], reasons: list[str]
    ) -> None:
        manual_followups = [
            item
            for item in list(state.get("manual_followups") or [])
            if not (isinstance(item, dict) and item.get("source") == "desktop_design_feedback")
        ]
        seen = {
            str(item.get("reason") if isinstance(item, dict) else item) for item in manual_followups
        }
        for reason in reasons:
            if reason in seen:
                continue
            manual_followups.append({"source": "desktop_design_feedback", "reason": reason})
            seen.add(reason)
        if manual_followups:
            state["manual_followups"] = manual_followups
        else:
            state.pop("manual_followups", None)

    @staticmethod
    def _command_attempts_desktop_design(command: str) -> bool:
        lowered = command.lower()
        return any(
            token in lowered
            for token in (
                "plasma-apply-lookandfeel",
                "plasma-apply-colorscheme",
                "plasma-apply-desktoptheme",
                "plasma-apply-wallpaperimage",
                "lookandfeeltool",
                "gsettings set org.gnome.desktop.interface gtk-theme",
                "gsettings set org.gnome.desktop.interface color-scheme",
                "gsettings set org.gnome.desktop.background",
                "dconf write",
                "xfconf-query",
                "swww ",
                "hyprpaper",
                "nitrogen",
                "feh --bg",
                "colorscheme",
                "lookandfeel",
                "wallpaper",
            )
        )

    @staticmethod
    def _command_attempts_wallpaper(command: str) -> bool:
        lowered = command.lower()
        return any(
            token in lowered
            for token in (
                "wallpaper",
                "picture-uri",
                "plasma-apply-wallpaperimage",
                "swww img",
                "hyprpaper",
                "nitrogen --set",
                "feh --bg",
            )
        )

    def _record_desktop_design_expectations(self, command: str, state: dict[str, Any]) -> None:
        self._record_kde_design_expectations(command, state)
        self._record_gnome_design_expectations(command, state)
        self._record_xfce_design_expectations(command, state)

    def _record_kde_design_expectations(self, command: str, state: dict[str, Any]) -> None:
        for tokens in self._shell_command_segments(command):
            if not tokens:
                continue
            executable = Path(tokens[0]).name
            if executable.startswith("kwriteconfig"):
                self._record_kwriteconfig_expectation(tokens, state)
                continue
            if executable == "lookandfeeltool" and "-a" in tokens:
                value = self._shell_option_value(tokens, "-a")
                if value:
                    state["expected_kde_color_scheme"] = self._friendly_kde_theme_name(value)
                continue
            if (
                executable in {"plasma-apply-lookandfeel", "plasma-apply-colorscheme"}
                and len(tokens) > 1
            ):
                state["expected_kde_color_scheme"] = self._friendly_kde_theme_name(tokens[-1])
                continue
            if executable == "plasma-apply-desktoptheme" and len(tokens) > 1:
                state["expected_kde_color_scheme"] = self._friendly_kde_theme_name(tokens[-1])
                continue
            if executable == "plasma-apply-icons" and len(tokens) > 1:
                state["expected_kde_icon_theme"] = tokens[-1]
                continue
            if executable == "plasma-apply-cursortheme" and len(tokens) > 1:
                state["expected_kde_cursor_theme"] = tokens[-1]
                continue
            if executable == "plasma-apply-wallpaperimage" and len(tokens) > 1:
                state["expected_kde_wallpaper"] = tokens[-1]

    @staticmethod
    def _friendly_kde_theme_name(value: str) -> str:
        text = str(value or "").strip().strip("\"'")
        lower = text.lower()
        if "breezedark" in lower or "breeze-dark" in lower:
            return "Breeze Dark"
        if "breezelight" in lower or "breeze-light" in lower:
            return "Breeze Light"
        text = re.sub(r"^org\.kde\.", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\.desktop$", "", text, flags=re.IGNORECASE)
        text = re.sub(r"[-_]+", " ", text).strip()
        return text.title() if text else value

    @staticmethod
    def _record_gnome_design_expectations(command: str, state: dict[str, Any]) -> None:
        try:
            tokens = shlex.split(command)
        except ValueError:
            return
        for index, token in enumerate(tokens):
            if Path(token).name != "gsettings" or index + 4 >= len(tokens):
                continue
            if tokens[index + 1] != "set":
                continue
            schema = tokens[index + 2].lower()
            key = tokens[index + 3].lower()
            value = " ".join(tokens[index + 4 :]).strip()
            if not value:
                continue
            if schema == "org.gnome.desktop.interface":
                if key == "gtk-theme":
                    state["expected_gnome_gtk_theme"] = value
                elif key == "icon-theme":
                    state["expected_gnome_icon_theme"] = value
                elif key == "cursor-theme":
                    state["expected_gnome_cursor_theme"] = value
                elif key == "color-scheme" and "dark" in value.lower():
                    state.setdefault("expected_gnome_gtk_theme", "Adwaita-dark")
                elif key == "enable-animations":
                    state["expected_gnome_enable_animations"] = value
            elif schema == "org.gnome.desktop.background" and key in {
                "picture-uri",
                "picture-uri-dark",
            }:
                state["expected_gnome_wallpaper"] = value.removeprefix("file://")
                if key == "picture-uri-dark":
                    state["gnome_wallpaper_dark_uri_only"] = True

    @staticmethod
    def _record_xfce_design_expectations(command: str, state: dict[str, Any]) -> None:
        for tokens in SetupOrchestrator._shell_command_segments(command):
            if not tokens:
                continue
            executable = Path(tokens[0]).name
            if executable != "xfconf-query":
                continue
            channel = SetupOrchestrator._shell_option_value(tokens, "-c").lower()
            prop = SetupOrchestrator._shell_option_value(tokens, "-p").lower()
            value = SetupOrchestrator._shell_option_value(tokens, "-s")
            if not value:
                continue
            if channel == "xsettings":
                if prop == "/net/themename":
                    state["expected_xfce_gtk_theme"] = value
                elif prop == "/net/iconthemename":
                    state["expected_xfce_icon_theme"] = value
                elif prop == "/gtk/cursorthemename":
                    state["expected_xfce_cursor_theme"] = value
            elif channel == "xfce4-desktop" and prop.endswith(("/last-image", "/image-path")):
                state["expected_xfce_wallpaper"] = value.removeprefix("file://")
            elif channel == "xfwm4" and prop == "/general/use_compositing":
                state["expected_xfce_compositing"] = value

    @staticmethod
    def _shell_command_segments(command: str) -> list[list[str]]:
        try:
            tokens = shlex.split(command)
        except ValueError:
            return []
        segments: list[list[str]] = []
        current: list[str] = []
        separators = {"&&", "||", ";", "|"}
        for token in tokens:
            if token in separators:
                if current:
                    segments.append(current)
                    current = []
                continue
            if current and Path(token).name.startswith(("kwriteconfig", "plasma-apply-")):
                segments.append(current)
                current = [token]
                continue
            current.append(token)
        if current:
            segments.append(current)
        return segments

    @staticmethod
    def _record_kwriteconfig_expectation(tokens: list[str], state: dict[str, Any]) -> None:
        file_name = SetupOrchestrator._shell_option_value(tokens, "--file").lower()
        group = SetupOrchestrator._shell_option_value(tokens, "--group").lower()
        key = SetupOrchestrator._shell_option_value(tokens, "--key").lower()
        value = SetupOrchestrator._kwriteconfig_value(tokens)
        if not value:
            return
        if file_name.endswith("kdeglobals") and group == "icons" and key == "theme":
            state["expected_kde_icon_theme"] = value
        elif file_name.endswith("kdeglobals") and group == "general" and key == "colorscheme":
            state["expected_kde_color_scheme"] = value
        elif file_name.endswith("kcminputrc") and group == "mouse" and key == "cursortheme":
            state["expected_kde_cursor_theme"] = value
        elif file_name.endswith("kwinrc") and group == "plugins" and key == "wobblywindowsenabled":
            state["expected_kde_wobbly_windows"] = value
        elif file_name.endswith("kwinrc") and group == "plugins" and key == "magiclampenabled":
            state["expected_kde_magic_lamp"] = value
        elif "plasma-org.kde.plasma.desktop-appletsrc" in file_name and key in {
            "image",
            "wallpaper",
        }:
            state["expected_kde_wallpaper"] = value

    @staticmethod
    def _shell_option_value(tokens: list[str], option: str) -> str:
        try:
            index = tokens.index(option)
        except ValueError:
            return ""
        if index + 1 >= len(tokens):
            return ""
        return tokens[index + 1]

    @staticmethod
    def _kwriteconfig_value(tokens: list[str]) -> str:
        try:
            index = tokens.index("--key")
        except ValueError:
            return ""
        if index + 2 >= len(tokens):
            return ""
        return tokens[index + 2]

    def _command_timeout(self, command: str) -> int:
        command_lower = command.lower()
        if any(
            re.search(pattern, command_lower)
            for pattern in (
                r"\bdnf5?\b.*\b(?:install|groupinstall|group\s+install|upgrade|update|distro-sync)\b",
                r"\b(?:apt-get|apt)\b.*\b(?:install|dist-upgrade|full-upgrade|upgrade)\b",
                r"\bzypper\b.*\b(?:install|in|dup|update|up|patch)\b",
                r"\bapk\b.*\b(?:add|upgrade)\b",
                r"\bxbps-install\b",
                r"\bemerge\b",
            )
        ):
            return 1800
        if re.search(r"\b(?:pacman|yay|paru)\b.*(?:^|\s)-S(?![sil])", command):
            return 1800
        if any(
            token in command_lower
            for token in (
                "yay -s",
                "makepkg",
                "pacstrap",
                "debootstrap",
                "anaconda",
                "liveinst",
                "dnf install",
                "dnf5 install",
                "dnf group install",
            )
        ):
            return 1800
        if "reboot" in command_lower:
            return 60
        return 300

    @staticmethod
    def _is_reboot_command(command: str) -> bool:
        return bool(
            re.search(
                r"(^|[;&|]\s*)(?:sudo\s+)?(?:systemctl\s+)?reboot\b"
                r"|(^|[;&|]\s*)(?:sudo\s+)?shutdown\s+-r\b"
                r"|(^|[;&|]\s*)(?:sudo\s+)?(?:telinit|init)\s+6\b"
                r"|(^|[;&|]\s*)(?:sudo\s+)?kexec\s+-e\b"
                r"|(^|[;&|]\s*)(?:sudo\s+)?sh\s+-c\s+['\"]?echo\s+[a-z]*b\s*>\s*/proc/sysrq-trigger",
                command,
            )
        )

    @staticmethod
    def _is_live_target_unmount_command(command: str) -> bool:
        return bool(
            re.search(
                r"(^|[;&|]\s*)(?:sudo\s+)?umount\s+(?:-[A-Za-z]+\s+)*(?:/mnt|/target)\b",
                command,
            )
        )

    @staticmethod
    def _extract_created_user(command: str) -> str | None:
        kickstart_match = re.search(
            r"\buser\s+[^;\n]*--name(?:=|\s+)(?P<user>[a-z_][a-z0-9_-]{0,31})\b",
            command,
            re.IGNORECASE,
        )
        if kickstart_match:
            return kickstart_match.group("user")
        flags_with_values = {
            "-c",
            "--comment",
            "-d",
            "--home-dir",
            "-e",
            "--expiredate",
            "-f",
            "--inactive",
            "-g",
            "--gid",
            "--ingroup",
            "-G",
            "--groups",
            "-k",
            "--skel",
            "-p",
            "--password",
            "-s",
            "--shell",
            "-u",
            "--uid",
            "-Z",
            "--selinux-user",
            "-K",
            "--key",
        }
        for line in command.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            matches = []
            chroot_match = re.search(
                r"(?:^|[;&|]\s*)(?:sudo\s+)?(?:(?:arch-chroot|chroot)\s+\S+\s+)?"
                r"(?:useradd|adduser)\b(?P<tail>[^;&|]*)",
                line,
            )
            if chroot_match:
                matches.append(chroot_match.group("tail"))
            matches.extend(
                match.group("tail")
                for match in re.finditer(
                    r"(?:^|[;&|]\s*|['\"])(?:sudo\s+)?(?:useradd|adduser)\b(?P<tail>[^;&|'\"]*)",
                    line,
                )
            )
            for tail in matches:
                user = SetupOrchestrator._extract_created_user_from_tail(
                    tail,
                    flags_with_values=flags_with_values,
                )
                if user:
                    return user
        return None

    @staticmethod
    def _extract_created_user_from_tail(tail: str, *, flags_with_values: set[str]) -> str | None:
        try:
            tokens = shlex.split(tail)
        except ValueError:
            tokens = tail.split()
        for idx in range(len(tokens) - 1, -1, -1):
            token = tokens[idx].strip("\"'`")
            if not token or token.startswith("-"):
                continue
            if idx > 0 and tokens[idx - 1] in flags_with_values:
                continue
            if re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", token):
                return token
        return None

    def _build_summary(self) -> str:
        plan = self.ctx.plan
        apps = plan.apps if plan else []
        final_state = self.ctx.stage_state.get(SetupStage.FINAL_AUDIT.value, {})
        verified_names = {
            str(name).strip()
            for name in final_state.get("verified_installed_apps", []) or []
            if str(name).strip()
        }
        unverified_names = {
            str(name).strip()
            for name in final_state.get("unverified_installed_apps", []) or []
            if str(name).strip()
        }
        verified_installed = sorted(verified_names)
        alternative_entries = [
            item
            for item in final_state.get("verified_alternative_apps", []) or []
            if isinstance(item, dict)
            and str(item.get("app") or "").strip()
            and str(item.get("alternative") or item.get("target") or "").strip()
        ]
        alternative_app_names = {str(item.get("app") or "").strip() for item in alternative_entries}
        manual_followup: dict[str, str] = {}
        skipped: list[str] = []
        for app in apps:
            if app.status == PlanItemStatus.EXPLICITLY_SKIPPED:
                skipped.append(app.name)
                continue
            if app.name in alternative_app_names:
                continue
            if app.status == PlanItemStatus.MANUAL_FOLLOWUP:
                if app_manual_followup_satisfied_by_verified(
                    app.name,
                    app.package_name,
                    verified_names,
                ):
                    continue
                manual_followup[app.name] = app.reason or "manual follow-up"
                continue
            if app.status in {PlanItemStatus.COMPLETED, PlanItemStatus.INSTALLED}:
                if app.name in verified_names:
                    continue
                reason = app.reason or "not verified by final package-manager check"
                if app.name in unverified_names:
                    reason = app.reason or "not verified by final package-manager check"
                manual_followup[app.name] = reason
        stage_followup = []
        for stage_state in self.ctx.stage_state.values():
            for item in stage_state.get("manual_followups", []) or []:
                reason = str(item.get("reason", "") or "").strip()
                if reason:
                    stage_followup.append(reason)
        parts = [plan.target_summary if plan else "Remote setup completed."]
        if verified_installed:
            parts.append("Verified installed: " + ", ".join(verified_installed))
        if alternative_entries:
            parts.append(
                "Alternative installed: "
                + ", ".join(
                    sorted(
                        f"{str(item.get('app') or '').strip()} -> "
                        f"{str(item.get('alternative') or item.get('target') or '').strip()}"
                        for item in alternative_entries
                    )
                )
            )
        if manual_followup:
            parts.append("Manual follow-up: " + ", ".join(sorted(manual_followup)))
        if skipped:
            parts.append("Skipped: " + ", ".join(sorted(skipped)))
        if stage_followup:
            parts.append("Manual follow-up: " + " | ".join(sorted(set(stage_followup))))
        return ". ".join(part for part in parts if part).strip()
