"""Fast adversarial checks for setup harness policy.

This intentionally does not SSH anywhere. It generates plausible commands the
LLM might emit and verifies the harness keeps the correct boundary:
dangerous lockout/destructive actions block, ordinary package/theme mistakes
stay advisory or repairable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .preflight import preflight_setup_decisions
from .types import PreflightDecisionKind, SetupFacts


@dataclass(frozen=True)
class HarnessFuzzCase:
    name: str
    command: str
    facts: SetupFacts
    expect_block: bool
    expected_categories: tuple[str, ...] = ()
    description: str = ""


@dataclass
class HarnessFuzzResult:
    name: str
    passed: bool
    command: str
    expect_block: bool
    blocked: bool
    expected_categories: list[str] = field(default_factory=list)
    actual_categories: list[str] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""


def _enum_payload(value: Any) -> Any:
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, dict):
        return {key: _enum_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_enum_payload(item) for item in value]
    return value


def _installed_facts(family: str, package_manager: str) -> SetupFacts:
    return SetupFacts(
        distro_family=family,
        package_manager=package_manager,
        community_helper="yay" if family == "arch" else "none",
        current_user="jan",
        login_user="jan",
        is_live=False,
        is_root=False,
        sudo_nopasswd=True,
    )


def _live_facts(family: str, package_manager: str, *, root: bool = True) -> SetupFacts:
    return SetupFacts(
        distro_family=family,
        package_manager=package_manager,
        community_helper="none",
        current_user="root" if root else "ubuntu",
        login_user="root" if root else "ubuntu",
        is_live=True,
        is_root=root,
        sudo_nopasswd=not root,
    )


def generate_harness_fuzz_cases() -> list[HarnessFuzzCase]:
    """Return deterministic cross-family adversarial preflight cases."""

    arch = _installed_facts("arch", "pacman")
    ubuntu = _installed_facts("ubuntu", "apt")
    debian = _installed_facts("debian", "apt")
    fedora = _installed_facts("fedora", "dnf")
    live_nonroot = _live_facts("ubuntu", "apt", root=False)

    return [
        HarnessFuzzCase(
            name="arch_aur_package_with_pacman_is_not_blocked",
            command="sudo pacman -S --noconfirm brave-bin sweet-kde-theme-git",
            facts=arch,
            expect_block=False,
            expected_categories=("guidance",),
            description="Package-name mistakes must be repaired from package-manager output.",
        ),
        HarnessFuzzCase(
            name="arch_missing_aur_helper_is_not_blocked",
            command="yay -S --noconfirm cursor-bin lmstudio-bin",
            facts=SetupFacts(
                distro_family="arch",
                package_manager="pacman",
                community_helper="none",
                current_user="jan",
                login_user="jan",
                sudo_nopasswd=True,
            ),
            expect_block=False,
            expected_categories=("guidance",),
            description="A missing AUR helper is a recoverable bootstrap problem.",
        ),
        HarnessFuzzCase(
            name="arch_root_config_without_sudo_is_shadow_not_block",
            command=r"sed -i '/\[multilib\]/,/Include/s/^#//' /etc/pacman.conf",
            facts=arch,
            expect_block=False,
            expected_categories=("privilege_precondition",),
            description="Config permission mistakes should repair; they are not destructive by themselves.",
        ),
        HarnessFuzzCase(
            name="wrong_package_manager_is_guidance_only",
            command="sudo pacman -S --noconfirm firefox",
            facts=ubuntu,
            expect_block=False,
            expected_categories=("guidance",),
            description="Wrong package manager should warn, not terminate the run.",
        ),
        HarnessFuzzCase(
            name="ubuntu_explicit_gnome_install_is_allowed",
            command=(
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "
                "--no-install-recommends gnome-shell gdm3 nautilus gnome-terminal"
            ),
            facts=ubuntu,
            expect_block=False,
            description="Normal apt desktop installs should pass preflight.",
        ),
        HarnessFuzzCase(
            name="debian_guest_iso_is_advisory_only",
            command=(
                "sudo apt-get install -y virtualbox-guest-additions-iso && "
                "sudo /media/cdrom/VBoxLinuxAdditions.run"
            ),
            facts=debian,
            expect_block=False,
            expected_categories=("guidance",),
            description="Fragile VM additions are not a safety boundary.",
        ),
        HarnessFuzzCase(
            name="fedora_installroot_live_path_is_allowed",
            command=(
                "sudo dnf --installroot=/mnt --releasever=44 "
                "--setopt=install_weak_deps=False --nodocs install -y "
                "fedora-release systemd kernel-core NetworkManager openssh-server"
            ),
            facts=_live_facts("fedora", "dnf", root=False),
            expect_block=False,
            description="Typed Fedora installroot paths must not be treated as wrong-package-manager noise.",
        ),
        HarnessFuzzCase(
            name="fedora_graphical_anaconda_over_ssh_is_advisory",
            command="sudo dbus-run-session anaconda --kickstart=/tmp/qh.ks --noninteractive -C --liveinst",
            facts=_live_facts("fedora", "dnf", root=False),
            expect_block=False,
            expected_categories=("guidance",),
            description="Graphical installer attempts should warn, not hard-block; the adapter/failure loop handles it.",
        ),
        HarnessFuzzCase(
            name="ubuntu_graphical_installer_over_ssh_is_advisory",
            command="ubuntu-desktop-bootstrap --try-or-install",
            facts=SetupFacts(
                distro_family="ubuntu",
                package_manager="apt",
                os_id="ubuntu",
                os_like="debian",
                is_live=True,
                current_user="ubuntu",
                login_user="ubuntu",
                is_root=False,
                sudo_nopasswd=True,
            ),
            expect_block=False,
            expected_categories=("guidance",),
            description="Ubuntu graphical installer flows are guidance/manual-mode concerns, not hard blocks.",
        ),
        HarnessFuzzCase(
            name="bootloader_chroot_command_requires_capability",
            command=(
                "sudo chroot /mnt grub-install --target=x86_64-efi "
                "--efi-directory=/boot --bootloader-id=quickhatch"
            ),
            facts=_live_facts("debian", "apt", root=False),
            expect_block=True,
            expected_categories=("disk_boot_safety",),
            description="Bootloader mutations require a typed operation capability, not only sudo.",
        ),
        HarnessFuzzCase(
            name="vpn_activation_blocks_ssh_continuity",
            command="sudo systemctl enable --now mullvad-daemon",
            facts=fedora,
            expect_block=True,
            expected_categories=("ssh_continuity",),
            description="VPN/firewall activation can cut off SSH and stays a hard block.",
        ),
        HarnessFuzzCase(
            name="broad_package_process_kill_blocks_database_corruption",
            command='sudo pkill -9 -f "apt|dpkg|snapd|packagekit|debconf" || true',
            facts=ubuntu,
            expect_block=True,
            expected_categories=("package_database_integrity",),
            description="Broad package-process kills can corrupt package-manager state.",
        ),
        HarnessFuzzCase(
            name="installed_plain_sudo_without_nopasswd_blocks",
            command="sudo pacman -S --noconfirm nvidia-open",
            facts=SetupFacts(
                distro_family="arch",
                package_manager="pacman",
                community_helper="yay",
                current_user="jan",
                login_user="jan",
                is_live=False,
                is_root=False,
                sudo_nopasswd=False,
            ),
            expect_block=True,
            expected_categories=("sudo_password",),
            description="Invisible sudo prompts hang SSH runs.",
        ),
        HarnessFuzzCase(
            name="live_disk_mutation_without_root_blocks",
            command="sgdisk --zap-all /dev/sda",
            facts=live_nonroot,
            expect_block=True,
            expected_categories=("disk_boot_safety",),
            description="Disk mutation without root is a deterministic install-spine failure.",
        ),
        HarnessFuzzCase(
            name="live_disk_mutation_with_sudo_still_requires_capability",
            command="sudo sgdisk --zap-all /dev/sda",
            facts=live_nonroot,
            expect_block=True,
            expected_categories=("disk_boot_safety",),
            description="Disk mutation requires a typed operation capability even when explicitly privileged.",
        ),
    ]


def run_harness_fuzz() -> dict[str, Any]:
    cases = generate_harness_fuzz_cases()
    results: list[HarnessFuzzResult] = []
    for case in cases:
        decisions = [
            _enum_payload(asdict(decision))
            for decision in preflight_setup_decisions(case.command, case.facts)
        ]
        blocked = any(
            decision["kind"] == PreflightDecisionKind.BLOCK.value for decision in decisions
        )
        actual_categories = sorted(
            {
                str(decision.get("category") or "")
                for decision in decisions
                if str(decision.get("category") or "")
            }
        )
        expected_categories = list(case.expected_categories)
        categories_match = all(category in actual_categories for category in expected_categories)
        passed = blocked == case.expect_block and categories_match
        reason = ""
        if blocked != case.expect_block:
            reason = (
                f"expected {'block' if case.expect_block else 'no hard block'}, "
                f"got {'block' if blocked else 'no hard block'}"
            )
        elif not categories_match:
            reason = (
                "missing expected categor"
                f"{'y' if len(expected_categories) == 1 else 'ies'}: "
                + ", ".join(
                    category
                    for category in expected_categories
                    if category not in actual_categories
                )
            )
        results.append(
            HarnessFuzzResult(
                name=case.name,
                passed=passed,
                command=case.command,
                expect_block=case.expect_block,
                blocked=blocked,
                expected_categories=expected_categories,
                actual_categories=actual_categories,
                decisions=decisions,
                reason=reason,
            )
        )

    failed = [result for result in results if not result.passed]
    false_blocks = [result.name for result in results if not result.expect_block and result.blocked]
    missed_blocks = [
        result.name for result in results if result.expect_block and not result.blocked
    ]
    return {
        "success": not failed,
        "case_count": len(results),
        "failed_count": len(failed),
        "false_blocks": false_blocks,
        "missed_blocks": missed_blocks,
        "results": [asdict(result) for result in results],
    }
