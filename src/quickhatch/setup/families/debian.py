"""Debian deterministic live-install adapter."""

from __future__ import annotations

from quickhatch.config import UserProfile

from ..types import SetupFacts
from .base import BaseFamilyInstaller


class DebianInstaller(BaseFamilyInstaller):
    family = "debian"
    suite = "trixie"
    mirror = "http://deb.debian.org/debian"
    apt_get = "apt-get -o DPkg::Lock::Timeout=300"

    def prepare_live_environment(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts, profile
        return [
            f"sudo {self.apt_get} update",
            f"sudo env DEBIAN_FRONTEND=noninteractive {self.apt_get} install -y "
            "parted dosfstools e2fsprogs util-linux udev",
        ]

    def efi_mountpoint(self) -> str:
        return "/mnt/boot/efi"

    def install_base_system(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del profile
        deb_arch = self.debian_arch(facts)
        grub_pkg = "grub-efi-arm64" if deb_arch == "arm64" else "grub-efi-amd64"
        kernel_pkg = "linux-image-arm64" if deb_arch == "arm64" else "linux-image-amd64"
        return [
            f"sudo {self.apt_get} update",
            f"sudo {self.apt_get} install -y debootstrap arch-install-scripts {grub_pkg}",
            f"sudo debootstrap --arch={deb_arch} {self.suite} /mnt {self.mirror}",
            "sudo genfstab -U /mnt | sudo tee -a /mnt/etc/fstab >/dev/null",
            "for qh_bind in dev dev/pts proc sys run; do "
            "sudo mkdir -p /mnt/$qh_bind; "
            "mountpoint -q /mnt/$qh_bind || sudo mount --bind /$qh_bind /mnt/$qh_bind; "
            "done",
            "test -s /etc/resolv.conf && sudo cp -L /etc/resolv.conf /mnt/etc/resolv.conf || true",
            "sudo chroot /mnt sh -c '"
            f"{self.apt_get} update && "
            f"DEBIAN_FRONTEND=noninteractive {self.apt_get} install -y "
            f"{kernel_pkg} sudo network-manager openssh-server {grub_pkg}'",
            "sudo chroot /mnt systemctl enable NetworkManager ssh",
            "echo quickhatch | sudo tee /mnt/etc/hostname >/dev/null",
        ]

    def install_bootloader(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del profile
        efi_target = self.efi_grub_target(facts)
        fallback = self.efi_fallback_filename(facts)
        return [
            "for qh_bind in dev dev/pts proc sys run; do "
            "sudo mkdir -p /mnt/$qh_bind; "
            "mountpoint -q /mnt/$qh_bind || sudo mount --bind /$qh_bind /mnt/$qh_bind; "
            "done",
            "sudo chroot /mnt sh -c '"
            f"grub-install --target={efi_target} --efi-directory=/boot/efi --bootloader-id=quickhatch --recheck || true; "
            f"grub-install --target={efi_target} --efi-directory=/boot/efi --bootloader-id=quickhatch --removable --no-nvram --recheck; "
            "for qh_efi_dir in /boot/efi/EFI/BOOT /boot/efi/EFI/quickhatch /boot/efi/EFI/ubuntu; do "
            'mkdir -p "$qh_efi_dir"; '
            'printf "%s\\n" "search --file --set=root /boot/grub/grub.cfg" '
            '"configfile /boot/grub/grub.cfg" > "$qh_efi_dir/grub.cfg"; '
            "done; "
            "mkdir -p /boot/efi/boot/grub; "
            'printf "%s\\n" "search --file --set=root /boot/grub/grub.cfg" '
            '"configfile /boot/grub/grub.cfg" > /boot/efi/boot/grub/grub.cfg; '
            f"test -f /boot/efi/EFI/BOOT/{fallback}'",
            "sudo chroot /mnt update-grub",
            "sudo chroot /mnt update-initramfs -u -k all",
        ]

    def post_reboot_package_query(self, packages: list[str], facts: SetupFacts) -> str:
        del facts
        if not packages:
            return "dpkg-query -W"
        return "dpkg-query -W " + " ".join(packages)
