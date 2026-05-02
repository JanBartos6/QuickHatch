"""Ubuntu deterministic live-install adapter."""

from __future__ import annotations

from .debian import DebianInstaller


class UbuntuInstaller(DebianInstaller):
    family = "ubuntu"
    suite = "noble"
    mirror = "http://archive.ubuntu.com/ubuntu"

    def configure_network(self, facts, profile):  # noqa: ANN001
        commands = super().configure_network(facts, profile)
        commands.append(
            "sudo mkdir -p /mnt/etc/netplan && "
            "printf '%s\\n' 'network:' '  version: 2' '  renderer: NetworkManager' | "
            "sudo tee /mnt/etc/netplan/01-quickhatch-networkmanager.yaml >/dev/null"
        )
        return commands
