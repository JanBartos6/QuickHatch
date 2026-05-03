from __future__ import annotations

import pytest

from quickhatch.config import UserProfile
from quickhatch.setup.families import get_family_installer
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
