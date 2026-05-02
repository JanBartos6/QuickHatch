"""Deterministic family install adapters for the live-install spine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from quickhatch.config import UserProfile

from ..types import SetupFacts


@dataclass
class InstallSpine:
    family: str
    adapter_version: str
    commands: list[str] = field(default_factory=list)
    verification: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class FamilyInstaller(Protocol):
    family: str
    version: str

    def detect_live_iso(self, facts: SetupFacts) -> bool: ...

    def prepare_disk(self, facts: SetupFacts, profile: UserProfile) -> list[str]: ...

    def install_base_system(self, facts: SetupFacts, profile: UserProfile) -> list[str]: ...

    def configure_user_ssh_sudo(self, facts: SetupFacts, profile: UserProfile) -> list[str]: ...

    def configure_network(self, facts: SetupFacts, profile: UserProfile) -> list[str]: ...

    def install_bootloader(self, facts: SetupFacts, profile: UserProfile) -> list[str]: ...

    def pre_reboot_checks(self, facts: SetupFacts) -> list[str]: ...

    def post_reboot_package_query(self, packages: list[str], facts: SetupFacts) -> str: ...

    def service_query(self, services: list[str], facts: SetupFacts) -> str: ...

    def final_boot_audit(self, facts: SetupFacts) -> list[str]: ...

    def build_install_spine(self, facts: SetupFacts, profile: UserProfile) -> InstallSpine: ...


class BaseFamilyInstaller:
    family = "unknown"
    version = "2026.05.02"

    def detect_live_iso(self, facts: SetupFacts) -> bool:
        return bool(facts.is_live)

    def prepare_disk(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts, profile
        return [
            "sudo swapoff -a || true",
            "mountpoint -q /mnt && sudo umount -R /mnt || true",
            "DISK=${QUICKHATCH_TARGET_DISK:-/dev/sda}; "
            'sudo parted -s "$DISK" mklabel gpt mkpart ESP fat32 1MiB 513MiB '
            "set 1 esp on mkpart primary ext4 513MiB 100%",
            "DISK=${QUICKHATCH_TARGET_DISK:-/dev/sda}; "
            'sudo mkfs.fat -F32 "${DISK}1" && sudo mkfs.ext4 -F "${DISK}2"',
            "DISK=${QUICKHATCH_TARGET_DISK:-/dev/sda}; "
            'sudo mount "${DISK}2" /mnt && sudo mkdir -p /mnt/boot '
            '&& sudo mount "${DISK}1" /mnt/boot',
        ]

    def install_base_system(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts, profile
        return []

    def configure_user_ssh_sudo(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts, profile
        user = "jan"
        return [
            f"sudo chroot /mnt sh -c 'id {user} >/dev/null 2>&1 || useradd -m -G wheel,sudo,video,audio -s /bin/bash {user}'",
            f"sudo install -d -m 700 -o {user} -g {user} /mnt/home/{user}/.ssh",
            f"sudo cp /root/.ssh/authorized_keys /mnt/home/{user}/.ssh/authorized_keys",
            f"sudo chown {user}:{user} /mnt/home/{user}/.ssh/authorized_keys && sudo chmod 600 /mnt/home/{user}/.ssh/authorized_keys",
            "echo '%wheel ALL=(ALL:ALL) NOPASSWD: ALL' | sudo tee /mnt/etc/sudoers.d/99-quickhatch-nopasswd >/dev/null",
            "sudo chmod 440 /mnt/etc/sudoers.d/99-quickhatch-nopasswd",
        ]

    def configure_network(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts, profile
        return [
            "sudo mkdir -p /mnt/etc/NetworkManager/conf.d",
            "printf '%s\\n' '[keyfile]' 'unmanaged-devices=none' | "
            "sudo tee /mnt/etc/NetworkManager/conf.d/10-quickhatch-managed.conf >/dev/null",
        ]

    def install_bootloader(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts, profile
        return []

    def pre_reboot_checks(self, facts: SetupFacts) -> list[str]:
        del facts
        return [
            "test -s /mnt/etc/fstab",
            "find /mnt/boot -maxdepth 2 -type f \\( -name 'vmlinuz*' -o -name 'initramfs*' -o -name 'initrd*' \\) | grep -q .",
            "find /mnt/boot -maxdepth 4 -type f \\( -iname 'grub*.efi' -o -iname 'bootx64.efi' \\) | grep -q .",
            "grep -Eq '^(root|[^:]+):[^!*]' /mnt/etc/shadow",
        ]

    def post_reboot_package_query(self, packages: list[str], facts: SetupFacts) -> str:
        del facts
        return " ".join(packages)

    def service_query(self, services: list[str], facts: SetupFacts) -> str:
        del facts
        return "systemctl is-enabled " + " ".join(services)

    def final_boot_audit(self, facts: SetupFacts) -> list[str]:
        del facts
        return [
            "test -s /etc/fstab",
            "find /boot -maxdepth 3 -type f \\( -name 'vmlinuz*' -o -name 'initramfs*' -o -name 'initrd*' \\) | grep -q .",
            "systemctl is-system-running --wait || true",
        ]

    def build_install_spine(self, facts: SetupFacts, profile: UserProfile) -> InstallSpine:
        commands = [
            *self.prepare_disk(facts, profile),
            *self.install_base_system(facts, profile),
            *self.configure_user_ssh_sudo(facts, profile),
            *self.configure_network(facts, profile),
            *self.install_bootloader(facts, profile),
        ]
        return InstallSpine(
            family=self.family,
            adapter_version=self.version,
            commands=commands,
            verification=self.pre_reboot_checks(facts),
        )
