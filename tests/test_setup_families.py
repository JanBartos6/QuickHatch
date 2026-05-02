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
