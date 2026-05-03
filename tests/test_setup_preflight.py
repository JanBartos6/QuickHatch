from __future__ import annotations

from quickhatch.setup.preflight import (
    arch_community_helpers_in_command,
    preflight_setup_advisories,
    preflight_setup_command,
    preflight_setup_decisions,
)
from quickhatch.setup.types import PreflightDecisionKind, SetupFacts


def test_preflight_warns_pacman_for_aur_only_packages():
    facts = SetupFacts(package_manager="pacman", community_helper="yay")

    error = preflight_setup_command("sudo pacman -S --noconfirm brave-bin", facts)
    advisories = preflight_setup_advisories("sudo pacman -S --noconfirm brave-bin", facts)

    assert error is None
    assert any("community/AUR" in item for item in advisories)


def test_preflight_warns_yay_before_helper_exists():
    facts = SetupFacts(package_manager="pacman", community_helper="none")

    error = preflight_setup_command("yay -S --noconfirm brave-bin", facts)
    advisories = preflight_setup_advisories("yay -S --noconfirm brave-bin", facts)

    assert error is None
    assert any("no AUR helper" in item for item in advisories)


def test_preflight_detects_wrapped_aur_helper_before_helper_exists():
    facts = SetupFacts(package_manager="pacman", community_helper="none", is_root=True)

    command = "arch-chroot /mnt sudo -u jan bash -lc 'yay -S --noconfirm brave-bin'"
    error = preflight_setup_command(command, facts)
    advisories = preflight_setup_advisories(command, facts)

    assert error is None
    assert any("no AUR helper" in item for item in advisories)
    assert arch_community_helpers_in_command(command) == {"yay"}


def test_preflight_warns_wrong_aur_helper_when_another_helper_is_detected():
    facts = SetupFacts(package_manager="pacman", community_helper="yay")

    error = preflight_setup_command("paru -S --noconfirm brave-bin", facts)
    advisories = preflight_setup_advisories("paru -S --noconfirm brave-bin", facts)

    assert error is None
    assert any("detected AUR helper" in item for item in advisories)


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


def test_preflight_warns_ubuntu_desktop_metapackages_for_debootstrap_setup():
    facts = SetupFacts(package_manager="apt", distro_family="ubuntu", is_root=False)

    command = (
        "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "
        "--no-install-recommends ubuntu-desktop-minimal virtualbox-guest-x11"
    )
    error = preflight_setup_command(command, facts)
    advisories = preflight_setup_advisories(command, facts)

    assert error is None
    assert any("desktop metapackages" in item for item in advisories)


def test_preflight_allows_explicit_gnome_core_packages_on_apt():
    facts = SetupFacts(package_manager="apt", distro_family="ubuntu", is_root=False)

    assert (
        preflight_setup_command(
            "sudo apt-get install -y --no-install-recommends gnome-shell gdm3 ubuntu-session gnome-terminal nautilus",
            facts,
        )
        is None
    )


def test_preflight_warns_virtualbox_guest_additions_iso_installer():
    facts = SetupFacts(package_manager="apt", distro_family="debian")

    command = (
        "sudo apt-get install -y virtualbox-guest-additions-iso && "
        "sudo /media/cdrom/VBoxLinuxAdditions.run"
    )
    error = preflight_setup_command(command, facts)
    advisories = preflight_setup_advisories(command, facts)

    assert error is None
    assert any("Guest Additions ISO" in item for item in advisories)


def test_preflight_allows_distro_virtualbox_guest_utility_packages():
    facts = SetupFacts(package_manager="apt", distro_family="debian")

    assert (
        preflight_setup_command(
            "sudo apt-get install -y virtualbox-guest-utils virtualbox-guest-x11",
            facts,
        )
        is None
    )


def test_preflight_warns_standalone_shell_variable_assignment():
    facts = SetupFacts(package_manager="pacman", community_helper="none")

    command = "UCODE=$(grep -qi intel /proc/cpuinfo && echo intel-ucode || echo amd-ucode)"
    error = preflight_setup_command(command, facts)
    advisories = preflight_setup_advisories(command, facts)

    assert error is None
    assert any("fresh SSH shell" in item for item in advisories)


def test_preflight_allows_variable_assignment_combined_with_use():
    facts = SetupFacts(package_manager="pacman", community_helper="none", is_root=True)

    error = preflight_setup_command(
        'UCODE=$(grep -qi intel /proc/cpuinfo && echo intel-ucode || echo amd-ucode) && pacstrap -K /mnt base "$UCODE"',
        facts,
    )

    assert error is None


def test_preflight_warns_invalid_pacstrap_noconfirm_flag():
    facts = SetupFacts(package_manager="pacman", community_helper="none", is_root=True)

    error = preflight_setup_command("pacstrap -K --noconfirm /mnt base linux", facts)
    advisories = preflight_setup_advisories("pacstrap -K --noconfirm /mnt base linux", facts)

    assert error is None
    assert any("pacstrap" in item and "--noconfirm" in item for item in advisories)


def test_preflight_allows_package_install_without_activation():
    facts = SetupFacts(package_manager="pacman", community_helper="yay")

    assert preflight_setup_command("yay -S --noconfirm mullvad-vpn-bin", facts) is None


def test_preflight_warns_fake_plasma_dbus_session():
    facts = SetupFacts(package_manager="pacman", community_helper="yay")

    command = "dbus-run-session -- plasma-apply-wallpaperimage ~/wallpaper.jpg"
    error = preflight_setup_command(command, facts)
    advisories = preflight_setup_advisories(command, facts)

    assert error is None
    assert any("dbus-run-session" in item for item in advisories)


def test_preflight_warns_fedora_live_anaconda_over_ssh():
    facts = SetupFacts(package_manager="dnf", distro_family="fedora", is_live=True)

    command = "sudo dbus-run-session anaconda --kickstart=/tmp/qh.ks --noninteractive -C --liveinst"
    error = preflight_setup_command(command, facts)
    advisories = preflight_setup_advisories(command, facts)

    assert error is None
    assert any("Anaconda" in item for item in advisories)


def test_preflight_allows_fedora_live_installroot_path():
    facts = SetupFacts(package_manager="dnf", distro_family="fedora", is_live=True)

    assert (
        preflight_setup_command(
            "sudo dnf --installroot=/mnt --releasever=44 "
            "--setopt=install_weak_deps=False --nodocs install -y "
            "fedora-release systemd kernel-core NetworkManager openssh-server",
            facts,
        )
        is None
    )


def test_preflight_warns_ubuntu_live_interactive_installer_over_ssh():
    facts = SetupFacts(
        package_manager="apt",
        distro_family="debian",
        os_id="ubuntu",
        os_like="debian",
        is_live=True,
    )

    command = "ubuntu-desktop-bootstrap --try-or-install"
    error = preflight_setup_command(command, facts)
    advisories = preflight_setup_advisories(command, facts)

    assert error is None
    assert any("Ubuntu" in item and "installer" in item for item in advisories)


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
        sudo_nopasswd=True,
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
        sudo_nopasswd=True,
    )

    error = preflight_setup_command(
        "printf '%s\\n' 'options nvidia-drm modeset=1' | sudo tee /etc/modprobe.d/nvidia.conf >/dev/null",
        facts,
    )

    assert error is None


def test_preflight_warns_broad_arch_desktop_app_group():
    facts = SetupFacts(package_manager="pacman", community_helper="yay", is_root=True)

    command = "pacman -S --noconfirm plasma kde-applications papirus-icon-theme"
    error = preflight_setup_command(command, facts)
    advisories = preflight_setup_advisories(command, facts)

    assert error is None
    assert any("Broad Arch desktop" in item for item in advisories)


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


def test_preflight_blocks_plain_sudo_when_installed_user_lacks_nopasswd():
    facts = SetupFacts(
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        is_live=False,
        is_root=False,
        sudo_nopasswd=False,
    )

    error = preflight_setup_command("sudo pacman -S --noconfirm nvidia-open", facts)

    assert error
    assert "Passwordless sudo is not available" in error
    assert "sudo -S" in error


def test_preflight_allows_sudo_stdin_when_installed_user_lacks_nopasswd():
    facts = SetupFacts(
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        is_live=False,
        is_root=False,
        sudo_nopasswd=False,
        gpu_vendors=["nvidia"],
        gpu_inventory=["NVIDIA GeForce RTX 3080"],
    )

    error = preflight_setup_command(
        "printf '%s\\n' password | sudo -S pacman -S --noconfirm nvidia-open",
        facts,
    )

    assert error is None


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


def test_preflight_warns_arch_default_nvidia_packages_for_pre_turing_gpu():
    facts = SetupFacts(
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        is_root=False,
        sudo_nopasswd=True,
        gpu_vendors=["nvidia"],
        gpu_inventory=["NVIDIA GeForce GTX 1080"],
    )

    command = "sudo pacman -S --noconfirm nvidia-open"
    error = preflight_setup_command(command, facts)
    advisories = preflight_setup_advisories(command, facts)

    assert error is None
    assert any("pre-Turing NVIDIA" in item for item in advisories)


def test_preflight_allows_arch_nvidia_open_for_rtx_gpu():
    facts = SetupFacts(
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        is_root=False,
        sudo_nopasswd=True,
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


def test_typed_preflight_keeps_package_name_concerns_advisory():
    facts = SetupFacts(package_manager="pacman", community_helper="none")

    decisions = preflight_setup_decisions("yay -S --noconfirm brave-bin", facts)

    assert not any(item.kind == PreflightDecisionKind.BLOCK for item in decisions)
    assert any(item.kind == PreflightDecisionKind.ADVISORY for item in decisions)


def test_typed_preflight_shadows_privilege_precondition_instead_of_blocking():
    facts = SetupFacts(
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        is_root=False,
    )

    decisions = preflight_setup_decisions("pacman -S --noconfirm nvidia-open", facts)

    assert any(item.kind == PreflightDecisionKind.SHADOW for item in decisions)
    assert not any(item.kind == PreflightDecisionKind.BLOCK for item in decisions)


def test_typed_preflight_keeps_sudo_password_prompt_as_hard_block():
    facts = SetupFacts(
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        is_live=False,
        is_root=False,
        sudo_nopasswd=False,
    )

    decisions = preflight_setup_decisions("sudo pacman -S --noconfirm nvidia-open", facts)

    assert any(item.kind == PreflightDecisionKind.BLOCK for item in decisions)
    assert any(item.category == "sudo_password" for item in decisions)


def test_typed_preflight_keeps_disk_boot_safety_as_hard_block():
    facts = SetupFacts(
        package_manager="apt",
        distro_family="ubuntu",
        current_user="ubuntu",
        login_user="ubuntu",
        is_live=True,
        is_root=False,
        sudo_nopasswd=True,
    )

    decisions = preflight_setup_decisions("sgdisk --zap-all /dev/sda", facts)

    assert any(item.kind == PreflightDecisionKind.BLOCK for item in decisions)
    assert any(item.category == "disk_boot_safety" for item in decisions)
