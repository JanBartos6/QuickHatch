"""Debian deterministic live-install adapter."""

from __future__ import annotations

from quickhatch.config import UserProfile

from ..types import SetupFacts
from .base import BaseFamilyInstaller


class DebianInstaller(BaseFamilyInstaller):
    family = "debian"
    suite = "trixie"
    mirror = "http://deb.debian.org/debian"

    def install_base_system(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts, profile
        return [
            "sudo apt-get update",
            "sudo apt-get install -y debootstrap arch-install-scripts grub-efi-amd64",
            f"sudo debootstrap --arch=amd64 {self.suite} /mnt {self.mirror}",
            "sudo genfstab -U /mnt | sudo tee -a /mnt/etc/fstab >/dev/null",
            "sudo chroot /mnt sh -c 'apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y linux-image-amd64 sudo network-manager openssh-server grub-efi-amd64'",
            "sudo chroot /mnt systemctl enable NetworkManager ssh",
            "echo quickhatch | sudo tee /mnt/etc/hostname >/dev/null",
        ]

    def install_bootloader(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts, profile
        return [
            "sudo mount --bind /dev /mnt/dev && sudo mount --bind /proc /mnt/proc && sudo mount --bind /sys /mnt/sys",
            "sudo chroot /mnt grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=quickhatch --recheck",
            "sudo chroot /mnt grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=quickhatch --removable --no-nvram --recheck",
            "sudo chroot /mnt update-grub",
            "sudo chroot /mnt update-initramfs -u -k all",
        ]

    def post_reboot_package_query(self, packages: list[str], facts: SetupFacts) -> str:
        del facts
        if not packages:
            return "dpkg-query -W"
        return "dpkg-query -W " + " ".join(packages)
