from __future__ import annotations

from quickhatch.setup.preflight import preflight_setup_command
from quickhatch.setup.types import SetupFacts


def test_preflight_blocks_pacman_for_aur_only_packages():
    facts = SetupFacts(package_manager="pacman", community_helper="yay")

    error = preflight_setup_command("sudo pacman -S --noconfirm brave-bin", facts)

    assert error
    assert "AUR-only" in error


def test_preflight_blocks_yay_before_helper_exists():
    facts = SetupFacts(package_manager="pacman", community_helper="none")

    error = preflight_setup_command("yay -S --noconfirm brave-bin", facts)

    assert error
    assert "no AUR helper" in error


def test_preflight_blocks_vpn_firewall_activation():
    facts = SetupFacts(package_manager="pacman", community_helper="yay")

    error = preflight_setup_command("sudo systemctl enable --now mullvad-daemon", facts)

    assert error
    assert "VPN or firewall" in error


def test_preflight_allows_package_install_without_activation():
    facts = SetupFacts(package_manager="pacman", community_helper="yay")

    assert preflight_setup_command("yay -S --noconfirm mullvad-vpn-bin", facts) is None


def test_preflight_blocks_fake_plasma_dbus_session():
    facts = SetupFacts(package_manager="pacman", community_helper="yay")

    error = preflight_setup_command(
        "dbus-run-session -- plasma-apply-wallpaperimage ~/wallpaper.jpg", facts
    )

    assert error
    assert "dbus-run-session" in error


def test_preflight_blocks_root_owned_config_edit_without_sudo():
    facts = SetupFacts(
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        is_root=False,
    )

    error = preflight_setup_command(
        r"sed -i '/\[multilib\]/,/Include = \/etc\/pacman.d\/mirrorlist/ s/^#//' /etc/pacman.conf",
        facts,
    )

    assert error
    assert "root-owned path" in error


def test_preflight_allows_root_owned_config_edit_with_sudo():
    facts = SetupFacts(
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        is_root=False,
    )

    error = preflight_setup_command(
        r"sudo sed -i '/\[multilib\]/,/Include = \/etc\/pacman.d\/mirrorlist/ s/^#//' /etc/pacman.conf",
        facts,
    )

    assert error is None


def test_preflight_blocks_non_root_package_manager_mutation():
    facts = SetupFacts(
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        is_root=False,
    )

    error = preflight_setup_command("pacman -S --noconfirm nvidia-open", facts)

    assert error
    assert "needs root privileges" in error
