"""Debian deterministic live-install adapter."""

from __future__ import annotations

from quickhatch.config import UserProfile

from ..types import SetupFacts
from .base import BaseFamilyInstaller


class DebianInstaller(BaseFamilyInstaller):
    family = "debian"
    suite = "trixie"
    mirror = "http://deb.debian.org/debian"

    def prepare_live_environment(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts, profile
        return [
            "sudo apt-get update",
            "sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y "
            "parted dosfstools e2fsprogs util-linux udev",
        ]

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
            "grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=quickhatch --recheck || true; "
            "grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=quickhatch --removable --no-nvram --recheck; "
            "for qh_efi_dir in /boot/efi/EFI/BOOT /boot/efi/EFI/quickhatch /boot/efi/EFI/ubuntu; do "
            'mkdir -p "$qh_efi_dir"; '
            'printf "%s\\n" "search --file --set=root /boot/grub/grub.cfg" '
            '"configfile /boot/grub/grub.cfg" > "$qh_efi_dir/grub.cfg"; '
            "done; "
            "mkdir -p /boot/efi/boot/grub; "
            'printf "%s\\n" "search --file --set=root /boot/grub/grub.cfg" '
            '"configfile /boot/grub/grub.cfg" > /boot/efi/boot/grub/grub.cfg; '
            "test -f /boot/efi/EFI/BOOT/BOOTX64.EFI'",
            "sudo chroot /mnt update-grub",
            "sudo chroot /mnt update-initramfs -u -k all",
        ]

    def post_reboot_package_query(self, packages: list[str], facts: SetupFacts) -> str:
        del facts
        if not packages:
            return "dpkg-query -W"
        return "dpkg-query -W " + " ".join(packages)
