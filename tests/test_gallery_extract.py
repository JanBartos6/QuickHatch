"""Tests for gallery repo parsing and install-method detection."""

from __future__ import annotations

from pathlib import Path

from quickhatch.gallery.extract import (
    detect_install_method,
    extract_package_lists,
    parse_repo_metadata,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_detect_install_method_prefers_stow(tmp_path):
    _write(tmp_path / ".stow-local-ignore", ".git")
    _write(tmp_path / "install.sh", "#!/usr/bin/env bash\n")

    assert detect_install_method(tmp_path) == "stow"


def test_detect_install_method_supports_chezmoi_and_ansible(tmp_path):
    _write(tmp_path / ".chezmoi.toml", "[data]\n")
    assert detect_install_method(tmp_path) == "chezmoi"

    other = tmp_path / "ansible"
    _write(other / "playbook.yml", "- hosts: localhost\n")
    assert detect_install_method(other) == "ansible"


def test_extract_package_lists_and_metadata(tmp_path):
    _write(
        tmp_path / "README.md",
        "# Dotfiles\nGNOME with Catppuccin, Papirus icons, and Inter everywhere.\n",
    )
    _write(tmp_path / "pacman.txt", "gnome-shell\npapirus-icon-theme\n# comment\n")
    _write(tmp_path / "dnf.txt", "gnome-shell\n")
    _write(tmp_path / "apt.txt", "gnome-shell\n")
    _write(
        tmp_path / "install.sh",
        "pacman -S --needed gnome-tweaks\napt install gnome-shell-extension-manager\n",
    )

    packages = extract_package_lists(tmp_path)
    parsed = parse_repo_metadata(tmp_path, {"title": "[GNOME] Catppuccin", "body": "", "de": None})

    assert "gnome-shell" in packages["arch"]
    assert "gnome-shell-extension-manager" in packages["ubuntu"]
    assert parsed["de"] == "gnome"
    assert parsed["theme"] == "catppuccin"
    assert parsed["icons"] == "papirus"
    assert parsed["font_ui"] == "inter"


def test_parse_repo_metadata_reads_common_desktop_config_files(tmp_path):
    _write(tmp_path / "README.md", "# Dotfiles\nHyprland setup.\n")
    _write(
        tmp_path / ".config" / "waybar" / "style.css",
        '@define-color accent #cba6f7;\n* { font-family: "JetBrainsMono Nerd Font"; }\n',
    )

    parsed = parse_repo_metadata(tmp_path, {"title": "hyprland rice", "body": "", "de": None})

    assert parsed["de"] == "hyprland"
    assert parsed["font_ui"].startswith("jetbrainsmono")
