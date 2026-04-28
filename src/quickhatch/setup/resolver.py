"""Cross-distro package and service resolution helpers."""

from __future__ import annotations

import re
import shlex
from typing import Any

from quickhatch.config import UserProfile

from .types import SetupFacts, StrategyDecision

_NVIDIA_OPEN_KERNEL_SUPPORTED_RE = re.compile(
    r"\b(?:rtx|gtx\s*16\d{2}|tesla\s+t4|quadro\s+(?:rtx|t)\b|"
    r"(?:turing|ampere|ada|blackwell|hopper))\b",
    re.IGNORECASE,
)
_NVIDIA_PRE_TURING_RE = re.compile(
    r"\b(?:gtx\s*(?:10|9|7|6)\d{2}|gt\s*\d{3}|quadro\s+[kmp]\d{3,4}|"
    r"tesla\s+[kmp]\d{2,4}|maxwell|pascal|kepler|fermi)\b",
    re.IGNORECASE,
)
_ARCH_OFFICIAL_NVIDIA_PACKAGES = frozenset(
    {
        "nvidia",
        "nvidia-dkms",
        "nvidia-lts",
        "nvidia-lts-open",
        "nvidia-open",
        "nvidia-open-dkms",
    }
)


def detect_family(facts: SetupFacts) -> str:
    pm = (facts.package_manager or "unknown").lower()
    if pm == "pacman":
        return "arch"
    if pm == "apt":
        return "debian"
    if pm == "dnf":
        return "fedora"
    if pm == "zypper":
        return "opensuse"
    if pm == "apk":
        return "alpine"
    if pm == "xbps":
        return "void"
    if pm == "emerge":
        return "gentoo"
    return "unknown"


def repo_search_command(query: str, facts: SetupFacts) -> str:
    q = shlex.quote(query)
    pm = facts.package_manager
    if pm == "pacman":
        return f"pacman -Ss -- {q}"
    if pm == "apt":
        return f"apt-cache search -- {q}"
    if pm == "dnf":
        return f"dnf search -- {q}"
    if pm == "zypper":
        return f"zypper search --details {q}"
    if pm == "apk":
        return f"apk search {q}"
    if pm == "xbps":
        return f"xbps-query -Rs {q}"
    if pm == "emerge":
        return f"emerge --search {q}"
    return ""


def community_search_command(query: str, facts: SetupFacts) -> str:
    q = shlex.quote(query)
    if facts.package_manager == "pacman" and facts.community_helper in {"yay", "paru"}:
        return f"{facts.community_helper} -Ss -- {q}"
    return ""


def build_install_command(packages: list[str], source: str, facts: SetupFacts) -> str:
    if not packages:
        return ""
    quoted = " ".join(shlex.quote(p) for p in packages)
    pm = facts.package_manager
    if source == "community" and facts.package_manager == "pacman":
        helper = facts.community_helper
        if helper in {"yay", "paru"}:
            if facts.is_root and facts.login_user and facts.login_user != "root":
                return f"sudo -u {shlex.quote(facts.login_user)} {helper} -S --noconfirm {quoted}"
            return f"{helper} -S --noconfirm {quoted}"
        return ""
    if pm == "pacman":
        return f"sudo pacman -S --noconfirm {quoted}"
    if pm == "apt":
        return f"sudo apt-get install -y {quoted}"
    if pm == "dnf":
        return f"sudo dnf install -y {quoted}"
    if pm == "zypper":
        return f"sudo zypper --non-interactive install {quoted}"
    if pm == "apk":
        return f"sudo apk add {quoted}"
    if pm == "xbps":
        return f"sudo xbps-install -y {quoted}"
    if pm == "emerge":
        return f"sudo emerge --ask=n {quoted}"
    return ""


def build_service_enable_command(services: list[str], facts: SetupFacts) -> str:
    if not services:
        return ""
    quoted = " ".join(shlex.quote(s) for s in services)
    init = facts.init_system
    if init == "systemd":
        return f"sudo systemctl enable --now {quoted}"
    if init == "openrc":
        return " && ".join(f"sudo rc-update add {shlex.quote(s)} default" for s in services)
    if init == "runit":
        return " && ".join(
            f"sudo ln -sf /etc/sv/{shlex.quote(s)} /var/service/{shlex.quote(s)}" for s in services
        )
    return ""


def nvidia_open_kernel_supported(facts: SetupFacts) -> bool:
    """Best-effort local classification for NVIDIA's open kernel module path.

    NVIDIA's open kernel modules require Turing-or-newer GPUs. Unknown
    inventory stays permissive so the model can resolve current package policy
    from repo/web evidence instead of being blocked by a weak local guess.
    """
    inventory = " ".join(facts.gpu_inventory or [])
    if not inventory:
        return True
    if _NVIDIA_PRE_TURING_RE.search(inventory):
        return False
    if _NVIDIA_OPEN_KERNEL_SUPPORTED_RE.search(inventory):
        return True
    return True


def arch_nvidia_official_package_supported(facts: SetupFacts, packages: list[str]) -> bool:
    """Return False for Arch NVIDIA package plans that conflict with detected hardware."""

    if nvidia_open_kernel_supported(facts):
        return True
    return not any(package in _ARCH_OFFICIAL_NVIDIA_PACKAGES for package in packages)


def driver_strategy(profile: UserProfile, facts: SetupFacts) -> StrategyDecision:
    del profile
    vendors = {v.lower() for v in facts.gpu_vendors}
    family = detect_family(facts)
    if "nvidia" in vendors:
        if family == "arch":
            if not nvidia_open_kernel_supported(facts):
                return StrategyDecision(
                    strategy="nvidia_legacy_manual_followup",
                    confidence="manual_followup_required",
                    source="manual_followup",
                    packages=[],
                    notes=(
                        "Detected pre-Turing NVIDIA hardware. Do not assume nvidia-open; "
                        "resolve the current legacy/proprietary package path before installing."
                    ),
                )
            return StrategyDecision(
                strategy="official_nvidia_open",
                confidence="resolved_official",
                source="official",
                packages=["nvidia-open", "nvidia-utils", "nvidia-settings", "linux-headers"],
                services=["nvidia-persistenced"],
                notes="Prefer the current official Arch NVIDIA Open modules path and verify with nvidia-smi.",
            )
        if family == "debian":
            return StrategyDecision(
                strategy="official_nvidia",
                confidence="resolved_official",
                source="official",
                packages=["nvidia-driver"],
                notes="Verify exact non-free repository requirements before install.",
            )
        if family == "fedora":
            return StrategyDecision(
                strategy="rpmfusion_nvidia",
                confidence="manual_followup_required",
                source="manual_followup",
                notes="Fedora often needs RPM Fusion enablement before NVIDIA packages are available.",
            )
        return StrategyDecision(
            strategy="nvidia_manual_followup",
            confidence="manual_followup_required",
            source="manual_followup",
            notes="NVIDIA path needs distro-specific confirmation before execution.",
        )
    if "amd" in vendors:
        return StrategyDecision(
            strategy="mesa_default",
            confidence="resolved_official",
            source="official",
            packages=[],
            notes=(
                "Modern AMD usually works with distro-default Mesa and linux-firmware. "
                "Verify amdgpu binding for modern Radeon cards; resolve Vulkan/ROCm packages "
                "only when gaming or compute requirements need them."
            ),
        )
    if "intel" in vendors:
        return StrategyDecision(
            strategy="intel_default",
            confidence="resolved_official",
            source="official",
            packages=[],
            notes=(
                "Modern Intel usually works with distro-default Mesa and firmware. Verify i915 "
                "or xe binding, and resolve Vulkan/VA-API packages only when acceleration "
                "requirements need them."
            ),
        )
    return StrategyDecision(
        strategy="no_special_driver",
        confidence="resolved_official",
        source="official",
        packages=[],
        notes="No GPU-specific driver action required from detected facts.",
    )


def boot_repair_checks(facts: SetupFacts) -> list[str]:
    family = detect_family(facts)
    if family == "arch":
        return ["mkinitcpio -P", "grub-install", "grub-mkconfig -o /boot/grub/grub.cfg"]
    if family == "debian":
        return ["update-initramfs -u -k all", "update-grub"]
    if family == "fedora":
        return ["dracut --regenerate-all -f", "grub2-mkconfig -o /boot/grub2/grub.cfg"]
    return []


def desktop_runtime_capabilities(facts: SetupFacts) -> dict[str, Any]:
    return {
        "session_type": facts.session_type,
        "display_manager_enabled": facts.display_manager_enabled,
        "display_manager_active": facts.display_manager_active,
        "has_live_session": bool(facts.session_type),
        "can_apply_live": bool(facts.session_type and facts.displays),
        "desktop_runtime_state": facts.desktop_runtime_state.value,
    }
