from __future__ import annotations

from quickhatch.setup.preflight import arch_community_helpers_in_command, preflight_setup_command
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


def test_preflight_detects_wrapped_aur_helper_before_helper_exists():
    facts = SetupFacts(package_manager="pacman", community_helper="none", is_root=True)

    error = preflight_setup_command(
        "arch-chroot /mnt sudo -u jan bash -lc 'yay -S --noconfirm brave-bin'",
        facts,
    )

    assert error
    assert "no AUR helper" in error
    assert arch_community_helpers_in_command(
        "arch-chroot /mnt sudo -u jan bash -lc 'yay -S --noconfirm brave-bin'"
    ) == {"yay"}


def test_preflight_blocks_wrong_aur_helper_when_another_helper_is_detected():
    facts = SetupFacts(package_manager="pacman", community_helper="yay")

    error = preflight_setup_command("paru -S --noconfirm brave-bin", facts)

    assert error
    assert "detected AUR helper is `yay`" in error


def test_preflight_blocks_vpn_firewall_activation():
    facts = SetupFacts(package_manager="pacman", community_helper="yay")

    error = preflight_setup_command("sudo systemctl enable --now mullvad-daemon", facts)

    assert error
    assert "VPN or firewall" in error


def test_preflight_blocks_broad_package_process_kill():
    facts = SetupFacts(package_manager="apt", distro_family="ubuntu", is_root=False)

    error = preflight_setup_command(
        'sudo pkill -9 -f "apt|dpkg|snapd|packagekit|debconf" || true',
        facts,
    )

    assert error
    assert "broad `pkill`" in error
    assert "dpkg half-written" in error


def test_preflight_allows_specific_non_package_pkill():
    facts = SetupFacts(package_manager="apt", distro_family="ubuntu", is_root=False)

    assert preflight_setup_command("sudo pkill -TERM my-temporary-helper", facts) is None


def test_preflight_blocks_standalone_shell_variable_assignment():
    facts = SetupFacts(package_manager="pacman", community_helper="none")

    error = preflight_setup_command(
        "UCODE=$(grep -qi intel /proc/cpuinfo && echo intel-ucode || echo amd-ucode)",
        facts,
    )

    assert error
    assert "variables do not persist" in error


def test_preflight_allows_variable_assignment_combined_with_use():
    facts = SetupFacts(package_manager="pacman", community_helper="none", is_root=True)

    error = preflight_setup_command(
        'UCODE=$(grep -qi intel /proc/cpuinfo && echo intel-ucode || echo amd-ucode) && pacstrap -K /mnt base "$UCODE"',
        facts,
    )

    assert error is None


def test_preflight_blocks_invalid_pacstrap_noconfirm_flag():
    facts = SetupFacts(package_manager="pacman", community_helper="none", is_root=True)

    error = preflight_setup_command("pacstrap -K --noconfirm /mnt base linux", facts)

    assert error
    assert "pacstrap` does not accept `--noconfirm" in error


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


def test_preflight_blocks_fedora_live_anaconda_over_ssh():
    facts = SetupFacts(package_manager="dnf", distro_family="fedora", is_live=True)

    error = preflight_setup_command(
        "sudo dbus-run-session anaconda --kickstart=/tmp/qh.ks --noninteractive -C --liveinst",
        facts,
    )

    assert error
    assert "graphical installer" in error
    assert "configure-only" in error


def test_preflight_blocks_ubuntu_live_interactive_installer_over_ssh():
    facts = SetupFacts(
        package_manager="apt",
        distro_family="debian",
        os_id="ubuntu",
        os_like="debian",
        is_live=True,
    )

    error = preflight_setup_command("ubuntu-desktop-bootstrap --try-or-install", facts)

    assert error
    assert "graphical/live installer" in error
    assert "debootstrap" in error


def test_preflight_allows_ubuntu_live_debootstrap_and_autoinstall():
    facts = SetupFacts(
        package_manager="apt",
        distro_family="debian",
        os_id="ubuntu",
        os_like="debian",
        is_live=True,
        is_root=False,
        sudo_nopasswd=True,
    )

    assert (
        preflight_setup_command(
            "sudo debootstrap --arch=amd64 noble /mnt http://archive.ubuntu.com/ubuntu",
            facts,
        )
        is None
    )
    assert (
        preflight_setup_command(
            "sudo subiquity --autoinstall-url=file:///tmp/user-data",
            facts,
        )
        is None
    )


def test_preflight_blocks_live_disk_mutation_without_root_privileges():
    facts = SetupFacts(
        package_manager="apt",
        distro_family="debian",
        os_id="ubuntu",
        is_live=True,
        is_root=False,
        sudo_nopasswd=True,
        current_user="ubuntu",
        login_user="ubuntu",
    )

    error = preflight_setup_command("sgdisk --zap-all /dev/sda", facts)

    assert error
    assert "needs root privileges" in error


def test_preflight_blocks_live_mount_without_root_privileges():
    facts = SetupFacts(
        package_manager="apt",
        distro_family="debian",
        os_id="ubuntu",
        is_live=True,
        is_root=False,
        sudo_nopasswd=True,
        current_user="ubuntu",
        login_user="ubuntu",
    )

    error = preflight_setup_command("mount --bind /dev /mnt/dev", facts)

    assert error
    assert "needs root privileges" in error


def test_preflight_allows_read_only_mount_diagnostics_without_root():
    facts = SetupFacts(
        package_manager="apt",
        distro_family="debian",
        os_id="ubuntu",
        is_live=True,
        is_root=False,
        sudo_nopasswd=True,
        current_user="ubuntu",
        login_user="ubuntu",
    )

    assert preflight_setup_command("mount", facts) is None
    assert preflight_setup_command("mount | grep ' /mnt '", facts) is None
    assert preflight_setup_command("mount -l | grep ext4", facts) is None


def test_preflight_allows_live_disk_mutation_with_sudo():
    facts = SetupFacts(
        package_manager="apt",
        distro_family="debian",
        os_id="ubuntu",
        is_live=True,
        is_root=False,
        sudo_nopasswd=True,
        current_user="ubuntu",
        login_user="ubuntu",
    )

    assert preflight_setup_command("sudo sgdisk --zap-all /dev/sda", facts) is None
    assert preflight_setup_command("sudo mount --bind /dev /mnt/dev", facts) is None


def test_preflight_allows_fedora_installed_anaconda_probe():
    facts = SetupFacts(package_manager="dnf", distro_family="fedora", is_live=False)

    error = preflight_setup_command("anaconda --help", facts)

    assert error is None


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


def test_preflight_blocks_root_owned_shell_redirection_without_root_shell():
    facts = SetupFacts(
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        is_root=False,
    )

    error = preflight_setup_command(
        "echo -e '[Autologin]\\nUser=jan' > /etc/sddm.conf.d/autologin.conf",
        facts,
    )

    assert error
    assert "redirects shell output" in error
    assert "sudo tee" in error


def test_preflight_allows_root_owned_shell_redirection_for_root_session():
    facts = SetupFacts(
        package_manager="pacman",
        community_helper="none",
        current_user="root",
        login_user="root",
        is_root=True,
    )

    error = preflight_setup_command(
        "echo -e '[Autologin]\\nUser=jan' > /etc/sddm.conf.d/autologin.conf",
        facts,
    )

    assert error is None


def test_preflight_allows_sudo_tee_for_root_owned_write():
    facts = SetupFacts(
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        is_root=False,
    )

    error = preflight_setup_command(
        "printf '%s\\n' 'options nvidia-drm modeset=1' | sudo tee /etc/modprobe.d/nvidia.conf >/dev/null",
        facts,
    )

    assert error is None


def test_preflight_blocks_broad_arch_desktop_app_group():
    facts = SetupFacts(package_manager="pacman", community_helper="yay", is_root=True)

    error = preflight_setup_command(
        "pacman -S --noconfirm plasma kde-applications papirus-icon-theme",
        facts,
    )

    assert error
    assert "broad Arch desktop application groups" in error


def test_preflight_blocks_non_root_runuser_wrapper():
    facts = SetupFacts(
        package_manager="pacman",
        community_helper="yay",
        current_user="linus",
        login_user="linus",
        is_root=False,
    )

    error = preflight_setup_command(
        "runuser -u linus -- kwriteconfig6 --file kdeglobals --group Icons --key Theme Papirus",
        facts,
    )

    assert error
    assert "may not be used by a non-root setup session" in error


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


def test_preflight_allows_non_root_package_manager_read_only_probes():
    facts = SetupFacts(
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        is_root=False,
    )

    assert preflight_setup_command("pacman -Q nvidia-open", facts) is None
    assert preflight_setup_command("pacman -Ss nvidia", facts) is None


def test_preflight_blocks_arch_default_nvidia_packages_for_pre_turing_gpu():
    facts = SetupFacts(
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        is_root=False,
        gpu_vendors=["nvidia"],
        gpu_inventory=["NVIDIA GeForce GTX 1080"],
    )

    error = preflight_setup_command("sudo pacman -S --noconfirm nvidia-open", facts)

    assert error
    assert "pre-Turing NVIDIA" in error


def test_preflight_allows_arch_nvidia_open_for_rtx_gpu():
    facts = SetupFacts(
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        is_root=False,
        gpu_vendors=["nvidia"],
        gpu_inventory=["NVIDIA GeForce RTX 3080"],
    )

    assert preflight_setup_command("sudo pacman -S --noconfirm nvidia-open", facts) is None


def test_preflight_still_blocks_pacman_database_refresh_without_root():
    facts = SetupFacts(
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        is_root=False,
    )

    error = preflight_setup_command("pacman -Sy", facts)

    assert error
    assert "needs root privileges" in error


def test_preflight_does_not_split_package_probe_inside_quoted_payload():
    facts = SetupFacts(
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        is_root=False,
    )

    assert preflight_setup_command("bash -c 'pacman -Q nvidia-open && echo ok'", facts) is None
