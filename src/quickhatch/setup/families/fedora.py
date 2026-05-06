"""Fedora deterministic live-install adapter."""

from __future__ import annotations

from quickhatch.config import UserProfile

from ..types import SetupFacts
from .base import BaseFamilyInstaller


class FedoraInstaller(BaseFamilyInstaller):
    family = "fedora"

    def prepare_live_environment(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts, profile
        return [
            "sudo setenforce 0 2>/dev/null || true",
            "sudo dnf install -y parted dosfstools e2fsprogs util-linux systemd-udev",
        ]

    def efi_mountpoint(self) -> str:
        return "/mnt/boot/efi"

    def install_base_system(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts, profile
        disk_vars = self.target_disk_partition_vars()
        return [
            "for qh_bind in dev dev/pts proc sys run; do "
            "sudo mkdir -p /mnt/$qh_bind; "
            "mountpoint -q /mnt/$qh_bind || sudo mount --bind /$qh_bind /mnt/$qh_bind; "
            "done",
            "sudo dnf --installroot=/mnt --use-host-config --releasever=$(rpm -E %fedora) "
            "--setopt=install_weak_deps=False --nodocs install -y "
            "fedora-release systemd kernel-core kernel-modules sudo dnf rpm policycoreutils "
            "NetworkManager openssh-server grub2-efi-x64 grub2-tools shim efibootmgr",
            "sudo dnf --installroot=/mnt clean all",
            "sudo mkdir -p /mnt/etc",
            "sudo sh -c 'findmnt -R /mnt -o SOURCE,TARGET,FSTYPE,OPTIONS -n > /tmp/qh-mounts'",
            disk_vars
            + 'ROOT_UUID=$(sudo blkid -s UUID -o value "$ROOT") && '
            + 'printf "UUID=%s / ext4 defaults 0 1\\n" "$ROOT_UUID" | '
            + "sudo tee /mnt/etc/fstab >/dev/null",
            disk_vars
            + 'ESP_UUID=$(sudo blkid -s UUID -o value "$ESP") && '
            + 'printf "UUID=%s /boot/efi vfat umask=0077 0 2\\n" "$ESP_UUID" | '
            + "sudo tee -a /mnt/etc/fstab >/dev/null",
            "echo quickhatch | sudo tee /mnt/etc/hostname >/dev/null",
            "sudo chroot /mnt systemctl enable NetworkManager sshd",
            "sudo touch /mnt/.autorelabel || true",
        ]

    def install_bootloader(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts, profile
        return [
            "for qh_bind in dev dev/pts proc sys run; do "
            "sudo mkdir -p /mnt/$qh_bind; "
            "mountpoint -q /mnt/$qh_bind || sudo mount --bind /$qh_bind /mnt/$qh_bind; "
            "done",
            "sudo chroot /mnt sh -c '"
            "mkdir -p /boot/efi/EFI/BOOT; "
            "if [ -f /boot/efi/EFI/fedora/shimx64.efi ]; then "
            "cp -f /boot/efi/EFI/fedora/shimx64.efi /boot/efi/EFI/BOOT/BOOTX64.EFI; "
            "elif [ -f /boot/efi/EFI/fedora/grubx64.efi ]; then "
            "cp -f /boot/efi/EFI/fedora/grubx64.efi /boot/efi/EFI/BOOT/BOOTX64.EFI; "
            "else "
            "grub2-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=quickhatch --removable --no-nvram --recheck; "
            "fi; "
            "test -f /boot/efi/EFI/BOOT/BOOTX64.EFI'",
            "sudo chroot /mnt dracut --regenerate-all -f",
            "sudo chroot /mnt grub2-mkconfig -o /boot/grub2/grub.cfg",
        ]

    def post_reboot_package_query(self, packages: list[str], facts: SetupFacts) -> str:
        del facts
        if not packages:
            return "rpm -qa"
        return "rpm -q " + " ".join(packages)
