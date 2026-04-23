from __future__ import annotations

from quickhatch.config import UserProfile
from quickhatch.setup.resolver import (
    build_install_command,
    build_service_enable_command,
    community_search_command,
    detect_family,
    driver_strategy,
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
    facts = SetupFacts(package_manager="pacman", gpu_vendors=["nvidia"])
    strategy = driver_strategy(UserProfile(), facts)

    assert strategy.strategy == "official_nvidia_open"
    assert strategy.confidence == "resolved_official"
    assert "nvidia-open" in strategy.packages
