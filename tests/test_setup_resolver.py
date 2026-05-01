from __future__ import annotations

from quickhatch.config import UserProfile
from quickhatch.setup.resolver import (
    arch_nvidia_official_package_supported,
    build_install_command,
    build_service_enable_command,
    community_search_command,
    detect_family,
    driver_strategy,
    nvidia_open_kernel_supported,
    repo_search_command,
)
from quickhatch.setup.types import SetupFacts


def test_detect_family_covers_supported_package_managers():
    assert detect_family(SetupFacts(package_manager="pacman")) == "arch"
    assert detect_family(SetupFacts(package_manager="apt")) == "debian"
    assert detect_family(SetupFacts(package_manager="dnf")) == "fedora"
    assert detect_family(SetupFacts(package_manager="zypper")) == "opensuse"
    assert detect_family(SetupFacts(package_manager="apk")) == "alpine"
    assert detect_family(SetupFacts(package_manager="xbps")) == "void"
    assert detect_family(SetupFacts(package_manager="emerge")) == "gentoo"


def test_repo_search_and_install_commands_are_typed_by_package_manager():
    facts = SetupFacts(package_manager="pacman", community_helper="yay", login_user="jan")
    assert repo_search_command("steam", facts) == "pacman -Ss -- steam"
    assert build_install_command(["steam", "discord"], "repo", facts) == (
        "sudo pacman -S --noconfirm steam discord"
    )
    assert community_search_command("brave", facts) == "yay -Ss -- brave"


def test_service_enable_command_respects_init_system():
    facts = SetupFacts(init_system="systemd")
    assert build_service_enable_command(["sddm", "NetworkManager"], facts) == (
        "sudo systemctl enable --now sddm NetworkManager"
    )


def test_arch_nvidia_driver_strategy_prefers_current_official_path():
    facts = SetupFacts(
        package_manager="pacman",
        gpu_vendors=["nvidia"],
        gpu_inventory=["NVIDIA GeForce RTX 3080"],
    )
    strategy = driver_strategy(UserProfile(), facts)

    assert strategy.strategy == "official_nvidia_open"
    assert strategy.confidence == "resolved_official"
    assert "nvidia-open" in strategy.packages


def test_arch_legacy_nvidia_driver_strategy_requires_current_research():
    facts = SetupFacts(
        package_manager="pacman",
        gpu_vendors=["nvidia"],
        gpu_inventory=["NVIDIA GeForce GTX 1080"],
    )
    strategy = driver_strategy(UserProfile(), facts)

    assert nvidia_open_kernel_supported(facts) is False
    assert strategy.strategy == "nvidia_legacy_manual_followup"
    assert strategy.confidence == "manual_followup_required"
    assert strategy.packages == []


def test_arch_nvidia_official_package_support_rejects_pascal_default_package():
    facts = SetupFacts(
        package_manager="pacman",
        gpu_vendors=["nvidia"],
        gpu_inventory=["NVIDIA GeForce GTX 1080"],
    )

    assert arch_nvidia_official_package_supported(facts, ["nvidia-open"]) is False
    assert arch_nvidia_official_package_supported(facts, ["nvidia-open-dkms"]) is False
    assert arch_nvidia_official_package_supported(facts, ["nvidia-utils"]) is True


def test_debian_and_ubuntu_nvidia_strategies_are_distinct():
    ubuntu = driver_strategy(
        UserProfile(),
        SetupFacts(
            package_manager="apt",
            os_id="ubuntu",
            gpu_vendors=["nvidia"],
            gpu_inventory=["NVIDIA GeForce RTX 3080"],
        ),
    )
    debian = driver_strategy(
        UserProfile(),
        SetupFacts(
            package_manager="apt",
            os_id="debian",
            gpu_vendors=["nvidia"],
            gpu_inventory=["NVIDIA GeForce RTX 3080"],
        ),
    )

    assert ubuntu.strategy == "ubuntu_drivers_nvidia"
    assert "ubuntu-drivers-common" in ubuntu.packages
    assert debian.strategy == "official_nvidia"
    assert "firmware-misc-nonfree" in debian.packages


def test_fedora_opensuse_and_void_nvidia_have_typed_paths_for_rtx():
    fedora = driver_strategy(
        UserProfile(),
        SetupFacts(
            package_manager="dnf",
            gpu_vendors=["nvidia"],
            gpu_inventory=["NVIDIA GeForce RTX 3080"],
        ),
    )
    opensuse = driver_strategy(
        UserProfile(),
        SetupFacts(
            package_manager="zypper",
            gpu_vendors=["nvidia"],
            gpu_inventory=["NVIDIA GeForce RTX 3080"],
        ),
    )
    void = driver_strategy(
        UserProfile(),
        SetupFacts(
            package_manager="xbps",
            gpu_vendors=["nvidia"],
            gpu_inventory=["NVIDIA GeForce RTX 3080"],
        ),
    )

    assert fedora.confidence == "resolved_community"
    assert "akmod-nvidia" in fedora.packages
    assert opensuse.confidence == "resolved_official"
    assert "nvidia-open-driver-G06-signed-kmp-meta" in opensuse.packages
    assert void.confidence == "resolved_official"
    assert "void-repo-nonfree" in void.packages


def test_alpine_and_gentoo_nvidia_remain_manual_followup():
    for package_manager in ["apk", "emerge"]:
        strategy = driver_strategy(
            UserProfile(),
            SetupFacts(
                package_manager=package_manager,
                gpu_vendors=["nvidia"],
                gpu_inventory=["NVIDIA GeForce RTX 3080"],
            ),
        )

        assert strategy.confidence == "manual_followup_required"
        assert strategy.source == "manual_followup"
        assert strategy.packages == []
