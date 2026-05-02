"""Arch Linux deterministic live-install adapter."""

from __future__ import annotations

from quickhatch.config import UserProfile

from ..types import SetupFacts
from .base import BaseFamilyInstaller


class ArchInstaller(BaseFamilyInstaller):
    family = "arch"

    def install_base_system(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts, profile
        return [
            "sudo pacman-key --init && sudo pacman-key --populate archlinux",
            "sudo pacman -Sy --noconfirm archlinux-keyring",
            "sudo pacstrap -K /mnt base linux linux-firmware sudo nano "
            "networkmanager openssh grub efibootmgr amd-ucode",
            "sudo genfstab -U /mnt | sudo tee -a /mnt/etc/fstab >/dev/null",
            'sudo arch-chroot /mnt sh -c "ln -sf /usr/share/zoneinfo/UTC /etc/localtime; hwclock --systohc"',
            "sudo arch-chroot /mnt sh -c \"sed -i 's/^#en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen; locale-gen; echo LANG=en_US.UTF-8 > /etc/locale.conf\"",
            "echo quickhatch | sudo tee /mnt/etc/hostname >/dev/null",
            "sudo arch-chroot /mnt systemctl enable NetworkManager sshd",
        ]

    def install_bootloader(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts, profile
        return [
            "sudo arch-chroot /mnt grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=quickhatch --recheck",
            "sudo arch-chroot /mnt grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=quickhatch --removable --no-nvram --recheck",
            "sudo arch-chroot /mnt grub-mkconfig -o /boot/grub/grub.cfg",
            "sudo arch-chroot /mnt mkinitcpio -P",
        ]

    def post_reboot_package_query(self, packages: list[str], facts: SetupFacts) -> str:
        del facts
        if not packages:
            return "pacman -Q"
        quoted = " ".join(packages)
        return f"pacman -Q {quoted}"
