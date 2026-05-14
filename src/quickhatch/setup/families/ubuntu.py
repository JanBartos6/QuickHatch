"""Ubuntu deterministic live-install adapter."""

from __future__ import annotations

from quickhatch.config import UserProfile

from ..types import SetupFacts
from .debian import DebianInstaller


class UbuntuInstaller(DebianInstaller):
    family = "ubuntu"
    suite = "noble"
    mirror = "http://archive.ubuntu.com/ubuntu"

    def install_base_system(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts, profile
        return [
            f"sudo {self.apt_get} update",
            f"sudo {self.apt_get} install -y debootstrap arch-install-scripts grub-efi-amd64",
            "QH_UBUNTU_SUITE=${QUICKHATCH_UBUNTU_SUITE:-}; "
            'if [ -z "$QH_UBUNTU_SUITE" ] && [ -r /etc/os-release ]; then '
            ". /etc/os-release; "
            'if [ "${ID:-}" = "ubuntu" ]; then '
            'QH_UBUNTU_SUITE="${VERSION_CODENAME:-}"; '
            "fi; "
            "fi; "
            f'QH_UBUNTU_SUITE="${{QH_UBUNTU_SUITE:-{self.suite}}}"; '
            f'sudo debootstrap --arch=amd64 "$QH_UBUNTU_SUITE" /mnt {self.mirror}',
            "sudo genfstab -U /mnt | sudo tee -a /mnt/etc/fstab >/dev/null",
            "for qh_bind in dev dev/pts proc sys run; do "
            "sudo mkdir -p /mnt/$qh_bind; "
            "mountpoint -q /mnt/$qh_bind || sudo mount --bind /$qh_bind /mnt/$qh_bind; "
            "done",
            "test -s /etc/resolv.conf && sudo cp -L /etc/resolv.conf /mnt/etc/resolv.conf || true",
            "sudo chroot /mnt sh -c '"
            f"{self.apt_get} update && "
            f"DEBIAN_FRONTEND=noninteractive {self.apt_get} install -y "
            "linux-generic sudo network-manager openssh-server grub-efi-amd64'",
            "sudo chroot /mnt systemctl enable NetworkManager ssh",
            "echo quickhatch | sudo tee /mnt/etc/hostname >/dev/null",
        ]

    def configure_network(self, facts, profile):  # noqa: ANN001
        commands = super().configure_network(facts, profile)
        commands.append(
            "sudo mkdir -p /mnt/etc/netplan && "
            "printf '%s\\n' 'network:' '  version: 2' '  renderer: NetworkManager' | "
            "sudo tee /mnt/etc/netplan/01-quickhatch-networkmanager.yaml >/dev/null"
        )
        return commands
