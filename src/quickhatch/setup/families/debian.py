"""Debian deterministic live-install adapter."""

from __future__ import annotations

from quickhatch.config import UserProfile

from ..types import SetupFacts
from .base import BaseFamilyInstaller


class DebianInstaller(BaseFamilyInstaller):
    family = "debian"
    suite = "trixie"
    mirror = "http://deb.debian.org/debian"

    def efi_mountpoint(self) -> str:
        return "/mnt/boot/efi"

    def install_base_system(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts, profile
        return [
            "sudo apt-get update",
            "sudo apt-get install -y debootstrap arch-install-scripts grub-efi-amd64",
            f"sudo debootstrap --arch=amd64 {self.suite} /mnt {self.mirror}",
            "sudo genfstab -U /mnt | sudo tee -a /mnt/etc/fstab >/dev/null",
            "for qh_bind in dev dev/pts proc sys run; do "
            "sudo mkdir -p /mnt/$qh_bind; "
            "mountpoint -q /mnt/$qh_bind || sudo mount --bind /$qh_bind /mnt/$qh_bind; "
            "done",
            "test -s /etc/resolv.conf && sudo cp -L /etc/resolv.conf /mnt/etc/resolv.conf || true",
            "sudo chroot /mnt sh -c 'apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y linux-image-amd64 sudo network-manager openssh-server grub-efi-amd64'",
            "sudo chroot /mnt systemctl enable NetworkManager ssh",
            "echo quickhatch | sudo tee /mnt/etc/hostname >/dev/null",
        ]

    def install_bootloader(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts, profile
        return [
            "for qh_bind in dev dev/pts proc sys run; do "
            "sudo mkdir -p /mnt/$qh_bind; "
            "mountpoint -q /mnt/$qh_bind || sudo mount --bind /$qh_bind /mnt/$qh_bind; "
            "done",
            "sudo chroot /mnt sh -c '"
            "grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=quickhatch --recheck "
            "|| grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=quickhatch --removable --no-nvram --recheck; "
            "test -f /boot/efi/EFI/BOOT/BOOTX64.EFI -o -f /boot/efi/EFI/quickhatch/grubx64.efi'",
            "sudo chroot /mnt update-grub",
            "sudo chroot /mnt update-initramfs -u -k all",
        ]

    def post_reboot_package_query(self, packages: list[str], facts: SetupFacts) -> str:
        del facts
        if not packages:
            return "dpkg-query -W"
        return "dpkg-query -W " + " ".join(packages)
