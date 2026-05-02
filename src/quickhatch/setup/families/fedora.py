"""Fedora deterministic live-install adapter."""

from __future__ import annotations

from quickhatch.config import UserProfile

from ..types import SetupFacts
from .base import BaseFamilyInstaller


class FedoraInstaller(BaseFamilyInstaller):
    family = "fedora"

    def install_base_system(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts, profile
        return [
            "sudo dnf --installroot=/mnt --releasever=$(rpm -E %fedora) "
            "--setopt=install_weak_deps=False --nodocs install -y "
            "fedora-release systemd kernel-core kernel-modules sudo NetworkManager "
            "openssh-server grub2-efi-x64 grub2-tools shim efibootmgr",
            "sudo dnf --installroot=/mnt clean all",
            "sudo mkdir -p /mnt/etc",
            "sudo sh -c 'findmnt -R /mnt -o SOURCE,TARGET,FSTYPE,OPTIONS -n > /tmp/qh-mounts'",
            "sudo sh -c 'blkid -s UUID -o value /dev/sda2 | "
            'awk "{print \\"UUID=\\"$1\\" / ext4 defaults 0 1\\"}" > /mnt/etc/fstab\'',
            "sudo sh -c 'blkid -s UUID -o value /dev/sda1 | "
            'awk "{print \\"UUID=\\"$1\\" /boot vfat umask=0077 0 2\\"}" >> /mnt/etc/fstab\'',
            "echo quickhatch | sudo tee /mnt/etc/hostname >/dev/null",
            "sudo chroot /mnt systemctl enable NetworkManager sshd",
        ]

    def install_bootloader(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts, profile
        return [
            "sudo mount --bind /dev /mnt/dev && sudo mount --bind /proc /mnt/proc && sudo mount --bind /sys /mnt/sys",
            "sudo chroot /mnt grub2-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=quickhatch --recheck || true",
            "sudo chroot /mnt grub2-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=quickhatch --removable --no-nvram --recheck || true",
            "sudo chroot /mnt grub2-mkconfig -o /boot/grub2/grub.cfg",
            "sudo chroot /mnt dracut --regenerate-all -f",
        ]

    def post_reboot_package_query(self, packages: list[str], facts: SetupFacts) -> str:
        del facts
        if not packages:
            return "rpm -qa"
        return "rpm -q " + " ".join(packages)
