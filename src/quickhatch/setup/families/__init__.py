"""Family adapter registry for deterministic setup spines."""

from __future__ import annotations

from .arch import ArchInstaller
from .base import BaseFamilyInstaller, FamilyInstaller
from .debian import DebianInstaller
from .fedora import FedoraInstaller
from .ubuntu import UbuntuInstaller


def get_family_installer(family: str) -> FamilyInstaller:
    normalized = (family or "unknown").lower()
    if normalized == "arch":
        return ArchInstaller()
    if normalized == "ubuntu":
        return UbuntuInstaller()
    if normalized == "debian":
        return DebianInstaller()
    if normalized == "fedora":
        return FedoraInstaller()
    if "ubuntu" in normalized:
        return UbuntuInstaller()
    if "debian" in normalized:
        return DebianInstaller()
    if "fedora" in normalized:
        return FedoraInstaller()
    return BaseFamilyInstaller()


__all__ = [
    "ArchInstaller",
    "BaseFamilyInstaller",
    "DebianInstaller",
    "FamilyInstaller",
    "FedoraInstaller",
    "UbuntuInstaller",
    "get_family_installer",
]
