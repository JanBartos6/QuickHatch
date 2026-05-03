"""Fedora deterministic live-install adapter."""

from __future__ import annotations

from quickhatch.config import UserProfile

from ..types import SetupFacts
from .base import BaseFamilyInstaller


class FedoraInstaller(BaseFamilyInstaller):
    family = "fedora"

    def install_base_system(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts, profile
        disk_vars = self.target_disk_partition_vars()
        return [
            "sudo dnf --installroot=/mnt --releasever=$(rpm -E %fedora) "
            "--setopt=install_weak_deps=False --nodocs install -y "
            "fedora-release systemd kernel-core kernel-modules sudo NetworkManager "
            "openssh-server grub2-efi-x64 grub2-tools shim efibootmgr",
            "sudo dnf --installroot=/mnt clean all",
            "sudo mkdir -p /mnt/etc",
            "sudo sh -c 'findmnt -R /mnt -o SOURCE,TARGET,FSTYPE,OPTIONS -n > /tmp/qh-mounts'",
            disk_vars
            + 'ROOT_UUID=$(sudo blkid -s UUID -o value "$ROOT") && '
            + 'printf "UUID=%s / ext4 defaults 0 1\\n" "$ROOT_UUID" | '
            + "sudo tee /mnt/etc/fstab >/dev/null",
            disk_vars
            + 'ESP_UUID=$(sudo blkid -s UUID -o value "$ESP") && '
            + 'printf "UUID=%s /boot vfat umask=0077 0 2\\n" "$ESP_UUID" | '
            + "sudo tee -a /mnt/etc/fstab >/dev/null",
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
