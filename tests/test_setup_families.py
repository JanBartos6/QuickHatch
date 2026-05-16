from __future__ import annotations

import pytest

from quickhatch.config import UserProfile
from quickhatch.setup.capabilities import dangerous_command_requirements
from quickhatch.setup.families import get_family_installer
from quickhatch.setup.operations import SetupOperation
from quickhatch.setup.types import SetupFacts


@pytest.mark.parametrize(
    ("family", "expected_probe"),
    [
        ("arch", "pacstrap"),
        ("ubuntu", "debootstrap"),
        ("debian", "debootstrap"),
        ("fedora", "dnf --installroot"),
    ],
)
def test_family_adapters_generate_deterministic_install_spines(family, expected_probe):
    installer = get_family_installer(family)
    spine = installer.build_install_spine(
        SetupFacts(distro_family=family, is_live=True),
        UserProfile(),
    )
    joined = "\n".join(spine.commands)

    assert spine.family == family
    assert spine.adapter_version
    assert expected_probe in joined
    assert spine.verification
    assert "QUICKHATCH_TARGET_DISK:?QuickHatch target disk is required" in joined
    assert 'case "$DISK" in *[0-9]) ESP="${DISK}p1"; ROOT="${DISK}p2"' in joined
    assert "-G wheel,sudo" not in joined
    assert "getent group wheel" in joined
    assert "getent group sudo" in joined
    assert "find /root /home -maxdepth 3 -path '*/.ssh/authorized_keys'" in joined
    assert "grep -Eq '^(root|[^:]+):[^!*]' /mnt/etc/shadow" not in "\n".join(spine.verification)
    assert "99-quickhatch-nopasswd" in "\n".join(spine.verification)


@pytest.mark.parametrize("family", ["arch", "ubuntu", "debian", "fedora"])
def test_family_install_spines_surface_all_tier1_capability_requirements(family):
    installer = get_family_installer(family)
    spine = installer.build_install_spine(
        SetupFacts(distro_family=family, is_live=True),
        UserProfile(),
    )
    kinds = {
        requirement["kind"]
        for command in spine.commands
        for requirement in dangerous_command_requirements(
            f"QUICKHATCH_TARGET_DISK=/dev/sda; {command}"
        )
    }

    assert {
        "disk_partition",
        "format_filesystem",
        "mount_target",
        "write_fstab",
        "bootloader_install",
    }.issubset(kinds)


@pytest.mark.parametrize(
    ("family", "query_probe"),
    [
        ("arch", "pacman -Q"),
        ("ubuntu", "dpkg-query -W"),
        ("debian", "dpkg-query -W"),
        ("fedora", "rpm -q"),
    ],
)
def test_family_adapters_generate_package_queries(family, query_probe):
    installer = get_family_installer(family)

    command = installer.post_reboot_package_query(["firefox"], SetupFacts(distro_family=family))

    assert command.startswith(query_probe)
    assert "firefox" in command


def test_fedora_adapter_fstab_uses_selected_target_disk_partitions():
    installer = get_family_installer("fedora")

    commands = installer.install_base_system(
        SetupFacts(distro_family="fedora", is_live=True),
        UserProfile(),
    )
    joined = "\n".join(commands)

    assert 'blkid -s UUID -o value "$ROOT"' in joined
    assert 'blkid -s UUID -o value "$ESP"' in joined
    assert "/dev/sda1" not in joined
    assert "/dev/sda2" not in joined


def test_fedora_installroot_reuses_live_repo_config():
    installer = get_family_installer("fedora")

    commands = installer.install_base_system(
        SetupFacts(distro_family="fedora", is_live=True),
        UserProfile(),
    )
    joined = "\n".join(commands)

    assert commands[0].startswith("for qh_bind in dev dev/pts proc sys run")
    assert "--installroot=/mnt --use-host-config" in joined
    assert " sudo dnf rpm policycoreutils " in joined


def test_fedora_live_install_sets_selinux_permissive_for_target_chroot_writes():
    installer = get_family_installer("fedora")

    commands = installer.prepare_live_environment(
        SetupFacts(distro_family="fedora", is_live=True),
        UserProfile(),
    )

    assert commands[0] == "sudo setenforce 0 2>/dev/null || true"


@pytest.mark.parametrize("family", ["ubuntu", "debian"])
def test_debian_family_bootloader_always_writes_removable_efi_fallback(family):
    installer = get_family_installer(family)

    commands = installer.install_bootloader(
        SetupFacts(distro_family=family, is_live=True),
        UserProfile(),
    )
    joined = "\n".join(commands)

    assert "--removable --no-nvram" in joined
    assert "|| true" in joined
    assert "test -f /boot/efi/EFI/BOOT/BOOTX64.EFI" in joined
    assert "/boot/efi/EFI/BOOT" in joined
    assert "/boot/efi/EFI/quickhatch" in joined
    assert "/boot/efi/EFI/ubuntu" in joined
    assert '> "$qh_efi_dir/grub.cfg"' in joined
    assert "/boot/efi/boot/grub/grub.cfg" in joined
    assert "configfile /boot/grub/grub.cfg" in joined
    assert "-o -f /boot/efi/EFI/quickhatch/grubx64.efi" not in joined


def test_ubuntu_adapter_uses_live_iso_codename_when_available():
    installer = get_family_installer("ubuntu")

    commands = installer.install_base_system(
        SetupFacts(distro_family="ubuntu", is_live=True),
        UserProfile(),
    )
    joined = "\n".join(commands)

    assert "QH_UBUNTU_SUITE" in joined
    assert 'if [ "${ID:-}" = "ubuntu" ]' in joined
    assert 'QH_UBUNTU_SUITE="${VERSION_CODENAME:-}"' in joined
    assert 'sudo debootstrap --arch=amd64 "$QH_UBUNTU_SUITE" /mnt' in joined
    assert ":-noble" in joined


@pytest.mark.parametrize("family", ["ubuntu", "debian"])
def test_debian_family_apt_commands_wait_for_package_manager_locks(family):
    installer = get_family_installer(family)

    spine = installer.build_install_spine(
        SetupFacts(distro_family=family, is_live=True),
        UserProfile(),
    )
    joined = "\n".join(spine.commands)

    assert "DPkg::Lock::Timeout=300" in joined
    assert "apt-get install -y" not in joined
    assert "apt-get -o DPkg::Lock::Timeout=300 install -y" in joined


@pytest.mark.parametrize(
    ("family", "package_probe"),
    [
        ("ubuntu", "parted dosfstools e2fsprogs"),
        ("debian", "parted dosfstools e2fsprogs"),
        ("fedora", "parted dosfstools e2fsprogs"),
    ],
)
def test_live_install_spine_installs_partition_tools_before_disk_mutation(
    family,
    package_probe,
):
    installer = get_family_installer(family)

    spine = installer.build_install_spine(
        SetupFacts(distro_family=family, is_live=True),
        UserProfile(),
    )
    joined = "\n".join(spine.commands)
    install_index = next(
        index for index, command in enumerate(spine.commands) if package_probe in command
    )
    parted_index = next(
        index for index, command in enumerate(spine.commands) if "parted -s" in command
    )

    assert install_index < parted_index
    assert "umount -Rl /mnt" in joined
    assert "mkfs.fat" in joined
    assert "mkfs.ext4" in joined


@pytest.mark.parametrize("family", ["arch", "ubuntu", "debian", "fedora"])
def test_family_adapters_render_common_typed_operations(family):
    installer = get_family_installer(family)
    profile = UserProfile(linux_user="alex")
    facts = SetupFacts(distro_family=family, is_live=True)

    format_commands = installer.render_operation(
        SetupOperation(kind="format_partition", target="/dev/sda2", filesystem="ext4"),
        facts,
        profile,
    )
    user_commands = installer.render_operation(
        SetupOperation(kind="create_user", target="alex"),
        facts,
        profile,
    )

    assert any("mkfs.ext4" in command for command in format_commands)
    assert any("useradd" in command for command in user_commands)
    assert "jan" not in "\n".join(user_commands)


def test_family_adapter_uses_explicit_username_note_when_ssh_user_is_root():
    installer = get_family_installer("arch")
    profile = UserProfile(linux_user="root", additional_info="my username is tom")

    spine = installer.build_install_spine(
        SetupFacts(distro_family="arch", is_live=True),
        profile,
    )
    joined = "\n".join(spine.commands)

    assert installer.target_user(profile) == "tom"
    assert "user=tom;" in joined
    assert "qh_user=tom;" in joined
    assert "/home/$qh_user/.ssh" in joined
    assert "user=quickhatch;" not in joined


def test_family_adapter_prefers_install_username_over_ssh_login_user():
    installer = get_family_installer("arch")
    profile = UserProfile(linux_user="root", install_username="tom")

    spine = installer.build_install_spine(
        SetupFacts(distro_family="arch", is_live=True),
        profile,
    )
    joined = "\n".join(spine.commands)

    assert installer.target_user(profile) == "tom"
    assert "user=tom;" in joined
    assert "qh_user=tom;" in joined
    assert "user=quickhatch;" not in joined


def test_family_adapter_does_not_parse_vague_user_notes_as_username():
    installer = get_family_installer("arch")

    assert (
        installer.target_user(
            UserProfile(
                linux_user="root",
                additional_info="Install user apps, NVIDIA driver, and the neon theme.",
            )
        )
        == "quickhatch"
    )
    assert (
        installer.target_user(UserProfile(linux_user="root", additional_info="username: root"))
        == "quickhatch"
    )
