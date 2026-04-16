"""Tests for the distro install primer loader."""

from __future__ import annotations

import pytest

from quickhatch.ai.install_primer import (
    list_supported_distros,
    load_family_text,
    load_primer_text,
    primer_prompt_block,
    resolve_primer,
    supported_distros_summary,
)


class TestFamilyStitch:
    """Primers for family-member distros should concatenate family + card."""

    def test_debian_family_loaded_for_ubuntu(self):
        text = load_primer_text("ubuntu")
        assert text is not None
        assert "Debian-family install procedure" in text
        assert "Ubuntu LTS card" in text
        # The mirror field should be the Ubuntu-specific one from the card
        assert "archive.ubuntu.com" in text

    def test_debian_family_loaded_for_kali(self):
        text = load_primer_text("kali")
        assert text is not None
        assert "Debian-family install procedure" in text
        assert "Kali Linux card" in text
        assert "kali-linux-default" in text  # distinctive Kali metapackage
        assert "http.kali.org" in text  # distinctive Kali mirror

    def test_arch_family_loaded_for_cachyos(self):
        text = load_primer_text("cachyos")
        assert text is not None
        assert "Arch-family install procedure" in text
        assert "CachyOS card" in text
        assert "linux-cachyos" in text  # distinctive kernel

    def test_fedora_family_loaded_for_bazzite(self):
        text = load_primer_text("bazzite")
        assert text is not None
        assert "Fedora-family install procedure" in text
        assert "Bazzite card" in text
        assert "ghcr.io/ublue-os/bazzite" in text  # distinctive ostree source

    def test_standalone_distro_has_no_family(self):
        """NixOS is standalone; primer should NOT include any family header."""
        text = load_primer_text("nixos")
        assert text is not None
        assert "install procedure (shared)" not in text
        assert "nixos-install" in text  # distinctive command

    def test_load_family_text_direct(self):
        assert "pacstrap" in (load_family_text("arch") or "")
        assert "debootstrap" in (load_family_text("debian") or "")
        assert "kickstart" in (load_family_text("fedora") or "").lower()

    def test_load_family_text_missing(self):
        assert load_family_text(None) is None
        assert load_family_text("nonexistent") is None

    def test_rocky_and_alma_share_primer_file(self):
        """Rocky and Alma both point at rhel-clones.md — both should load the same body."""
        rocky = load_primer_text("rocky")
        alma = load_primer_text("alma")
        assert rocky is not None and alma is not None
        assert rocky == alma  # exact same stitched output since same family + same file
        # Both should contain both distros' mirror URLs (it's a combined primer)
        assert "download.rockylinux.org" in rocky
        assert "repo.almalinux.org" in rocky

    def test_ubuntu_flavors_documented(self):
        """Official Ubuntu flavors listed in the Ubuntu card, not separate primers."""
        text = load_primer_text("ubuntu")
        assert text is not None
        for flavor in ("kubuntu-desktop", "xubuntu-desktop", "lubuntu-desktop"):
            assert flavor in text, f"Missing flavor metapackage: {flavor}"

    def test_artix_has_init_branching(self):
        """Artix primer must cover init choice since systemctl doesn't apply."""
        text = load_primer_text("artix")
        assert text is not None
        assert "openrc" in text.lower()
        assert "runit" in text.lower()
        # Explicit note that systemctl won't work
        assert "systemctl" in text.lower()


class TestListSupportedDistros:
    def test_expected_count(self):
        distros = list_supported_distros()
        # 20 primers shipped
        assert len(distros) >= 15, f"Too few primers: {len(distros)}"

    def test_each_entry_has_required_fields(self):
        for entry in list_supported_distros():
            assert "file" in entry
            assert "canonical" in entry
            assert "family" in entry
            assert "release_model" in entry
            assert "aliases" in entry
            assert isinstance(entry["aliases"], list)

    @pytest.mark.parametrize(
        "canonical",
        [
            "Arch Linux",
            "Fedora Workstation",
            "Ubuntu LTS",
            "Debian stable",
            "NixOS",
            "Linux Mint",
            "Pop!_OS",
            "openSUSE Tumbleweed",
            "Bazzite",
            "CachyOS",
            "Alpine Linux",
            "Void Linux",
            "Gentoo Linux",
            "Kali Linux",
            "Manjaro Linux",
            "Artix Linux",
            "MX Linux",
            "Parrot OS",
            "Rocky Linux",
            "AlmaLinux",
        ],
    )
    def test_major_distros_present(self, canonical):
        canonicals = {e["canonical"] for e in list_supported_distros()}
        assert canonical in canonicals


class TestResolvePrimer:
    @pytest.mark.parametrize(
        "hint,expected",
        [
            ("arch", "Arch Linux"),
            ("Arch", "Arch Linux"),
            ("Arch Linux", "Arch Linux"),
            ("archlinux", "Arch Linux"),
            ("arch-linux", "Arch Linux"),
            ("fedora", "Fedora Workstation"),
            ("Fedora", "Fedora Workstation"),
            ("ubuntu", "Ubuntu LTS"),
            ("Ubuntu", "Ubuntu LTS"),
            ("nixos", "NixOS"),
            ("NixOS", "NixOS"),
            ("nix", "NixOS"),
            ("mint", "Linux Mint"),
            ("linux-mint", "Linux Mint"),
            ("pop", "Pop!_OS"),
            ("popos", "Pop!_OS"),
            ("pop-os", "Pop!_OS"),
            ("pop!_os", "Pop!_OS"),
            ("tumbleweed", "openSUSE Tumbleweed"),
            ("openSUSE Tumbleweed", "openSUSE Tumbleweed"),
            ("leap", "openSUSE Leap"),
            ("bazzite", "Bazzite"),
            ("silverblue", "Fedora Silverblue"),
            ("kinoite", "Fedora Silverblue"),
            ("cachyos", "CachyOS"),
            ("endeavouros", "EndeavourOS"),
            ("eos", "EndeavourOS"),
            ("garuda", "Garuda Linux"),
            ("zorin", "Zorin OS"),
            ("elementary", "elementary OS"),
            ("kde-neon", "KDE Neon"),
            ("neon", "KDE Neon"),
            ("alpine", "Alpine Linux"),
            ("void", "Void Linux"),
            ("gentoo", "Gentoo Linux"),
            ("kali", "Kali Linux"),
            ("Kali", "Kali Linux"),
            ("kali-linux", "Kali Linux"),
            ("kali-rolling", "Kali Linux"),
            ("pentest", "Kali Linux"),
            ("manjaro", "Manjaro Linux"),
            ("Manjaro", "Manjaro Linux"),
            ("manjaro-linux", "Manjaro Linux"),
            ("artix", "Artix Linux"),
            ("Artix", "Artix Linux"),
            ("arch-openrc", "Artix Linux"),
            ("arch-runit", "Artix Linux"),
            ("no-systemd", "Artix Linux"),
            ("mx", "MX Linux"),
            ("mx-linux", "MX Linux"),
            ("mxlinux", "MX Linux"),
            ("parrot", "Parrot OS"),
            ("parrot-os", "Parrot OS"),
            ("parrot-security", "Parrot OS"),
            ("rocky", "Rocky Linux"),
            ("Rocky Linux", "Rocky Linux"),
            ("rocky-linux", "Rocky Linux"),
            ("rockylinux", "Rocky Linux"),
            ("alma", "AlmaLinux"),
            ("AlmaLinux", "AlmaLinux"),
            ("alma-linux", "AlmaLinux"),
            ("almalinux", "AlmaLinux"),
        ],
    )
    def test_alias_matches(self, hint, expected):
        entry = resolve_primer(hint)
        assert entry is not None, f"No match for hint {hint!r}"
        assert entry["canonical"] == expected

    def test_free_text_substring_match(self):
        """Loose user-typed hints should still resolve."""
        entry = resolve_primer("I'd really like arch btw")
        assert entry is not None
        assert entry["canonical"] == "Arch Linux"

    def test_unknown_distro_returns_none(self):
        assert resolve_primer("banana-os") is None
        assert resolve_primer("templeos") is None

    def test_empty_input(self):
        assert resolve_primer("") is None
        assert resolve_primer(None) is None
        assert resolve_primer("   ") is None


class TestLoadPrimerText:
    def test_loads_arch_primer(self):
        text = load_primer_text("arch")
        assert text is not None
        assert "Arch Linux" in text
        assert "pacstrap" in text
        assert "grub-install" in text

    def test_loads_nixos_primer(self):
        text = load_primer_text("nixos")
        assert text is not None
        assert "NixOS" in text
        assert "nixos-install" in text
        assert "configuration.nix" in text

    def test_loads_fedora_primer(self):
        text = load_primer_text("fedora")
        assert text is not None
        assert "Fedora" in text
        assert "kickstart" in text.lower() or "anaconda" in text.lower()

    def test_unknown_distro_returns_none(self):
        assert load_primer_text("banana") is None


class TestPrimerPromptBlock:
    def test_known_distro_wraps_with_header(self):
        block = primer_prompt_block("arch")
        assert "DISTRO INSTALL PRIMER: Arch Linux" in block
        assert "authoritative" in block.lower()
        assert "pacstrap" in block  # from the primer body

    def test_unknown_distro_emits_fallback_with_catalog(self):
        block = primer_prompt_block("banana-os")
        assert "DISTRO INSTALL PRIMER (not found)" in block
        assert "banana-os" in block
        # Fallback lists the supported distros so the agent knows what's available
        assert "QuickHatch-supported distros" in block
        assert "Arch Linux" in block

    def test_empty_distro_emits_fallback(self):
        block = primer_prompt_block("")
        assert "DISTRO INSTALL PRIMER (not found)" in block


class TestSupportedDistrosSummary:
    def test_renders_list(self):
        summary = supported_distros_summary()
        assert "QuickHatch-supported distros" in summary
        # One bullet per distro
        assert summary.count("- **") >= 15
