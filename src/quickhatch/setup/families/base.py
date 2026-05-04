"""Deterministic family install adapters for the live-install spine."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Protocol

from quickhatch.config import UserProfile, generate_setup_password

from ..operations import SetupOperation
from ..types import SetupFacts


@dataclass
class InstallSpine:
    family: str
    adapter_version: str
    commands: list[str] = field(default_factory=list)
    verification: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class FamilyInstaller(Protocol):
    family: str
    version: str

    def detect_live_iso(self, facts: SetupFacts) -> bool: ...

    def prepare_disk(self, facts: SetupFacts, profile: UserProfile) -> list[str]: ...

    def install_base_system(self, facts: SetupFacts, profile: UserProfile) -> list[str]: ...

    def configure_user_ssh_sudo(self, facts: SetupFacts, profile: UserProfile) -> list[str]: ...

    def configure_network(self, facts: SetupFacts, profile: UserProfile) -> list[str]: ...

    def install_bootloader(self, facts: SetupFacts, profile: UserProfile) -> list[str]: ...

    def pre_reboot_checks(self, facts: SetupFacts) -> list[str]: ...

    def post_reboot_package_query(self, packages: list[str], facts: SetupFacts) -> str: ...

    def service_query(self, services: list[str], facts: SetupFacts) -> str: ...

    def final_boot_audit(self, facts: SetupFacts) -> list[str]: ...

    def render_operation(
        self, operation: SetupOperation, facts: SetupFacts, profile: UserProfile
    ) -> list[str]: ...

    def build_install_spine(self, facts: SetupFacts, profile: UserProfile) -> InstallSpine: ...


class BaseFamilyInstaller:
    family = "unknown"
    version = "2026.05.02"

    @staticmethod
    def target_disk_partition_vars() -> str:
        """Define shell variables for target disk partitions across sdX/nvme/mmc names."""

        return (
            "DISK=${QUICKHATCH_TARGET_DISK:?QuickHatch target disk is required}; "
            'case "$DISK" in *[0-9]) ESP="${DISK}p1"; ROOT="${DISK}p2" ;; '
            '*) ESP="${DISK}1"; ROOT="${DISK}2" ;; esac; '
        )

    @staticmethod
    def shell_quote(command: str) -> str:
        return shlex.quote(command)

    @staticmethod
    def target_user(profile: UserProfile) -> str:
        candidate = (getattr(profile, "linux_user", "") or "").strip().lower()
        if candidate and candidate != "root" and re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", candidate):
            return candidate[:31]
        return "jan"

    def detect_live_iso(self, facts: SetupFacts) -> bool:
        return bool(facts.is_live)

    def efi_mountpoint(self) -> str:
        return "/mnt/boot"

    def prepare_disk(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts, profile
        disk_vars = self.target_disk_partition_vars()
        efi_mount = shlex.quote(self.efi_mountpoint())
        return [
            "sudo swapoff -a || true",
            "mountpoint -q /mnt && sudo umount -R /mnt || true",
            disk_vars + 'sudo parted -s "$DISK" mklabel gpt mkpart ESP fat32 1MiB 513MiB '
            "set 1 esp on mkpart primary ext4 513MiB 100%",
            disk_vars
            + 'sudo partprobe "$DISK" 2>/dev/null || true; '
            + 'sudo blockdev --rereadpt "$DISK" 2>/dev/null || true; '
            + "sudo udevadm settle 2>/dev/null || true; "
            + 'for qh_part in "$ESP" "$ROOT"; do '
            + 'for qh_i in $(seq 1 40); do [ -b "$qh_part" ] && break; '
            + "sleep 0.25; sudo udevadm settle 2>/dev/null || true; done; "
            + 'test -b "$qh_part"; '
            + "done",
            disk_vars + 'sudo mkfs.fat -F32 "$ESP" && sudo mkfs.ext4 -F "$ROOT"',
            disk_vars + f'sudo mount "$ROOT" /mnt && sudo mkdir -p {efi_mount} '
            f'&& sudo mount "$ESP" {efi_mount}',
        ]

    def install_base_system(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts, profile
        return []

    def configure_user_ssh_sudo(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts
        user = self.target_user(profile)
        user_q = shlex.quote(user)
        password = getattr(profile, "default_account_password", "") or generate_setup_password()
        root_chpasswd = shlex.quote(f"root:{password}")
        user_chpasswd = shlex.quote(f"{user}:{password}")
        return [
            "sudo chroot /mnt sh -c "
            + self.shell_quote(
                "set -e; "
                f"user={user}; "
                "qh_groups=; "
                "for qh_group in wheel sudo video audio input docker; do "
                'getent group "$qh_group" >/dev/null 2>&1 || continue; '
                'qh_groups="${qh_groups:+$qh_groups,}$qh_group"; '
                "done; "
                'if id "$user" >/dev/null 2>&1; then exit 0; fi; '
                'if [ -n "$qh_groups" ]; then '
                'useradd -m -G "$qh_groups" -s /bin/bash "$user"; '
                "else "
                'useradd -m -s /bin/bash "$user"; '
                "fi"
            ),
            "sudo chroot /mnt sh -c " + self.shell_quote(f"echo {root_chpasswd} | chpasswd"),
            "sudo chroot /mnt sh -c " + self.shell_quote(f"echo {user_chpasswd} | chpasswd"),
            f"qh_user={user_q}; "
            "qh_auth_src=/root/.ssh/authorized_keys; "
            'if [ ! -s "$qh_auth_src" ]; then '
            "qh_auth_src=$(find /root /home -maxdepth 3 -path '*/.ssh/authorized_keys' "
            "-size +0c -print -quit 2>/dev/null); "
            "fi; "
            'test -n "$qh_auth_src" && test -s "$qh_auth_src"; '
            'qh_uid=$(sudo chroot /mnt id -u "$qh_user"); '
            'qh_gid=$(sudo chroot /mnt id -g "$qh_user"); '
            'sudo install -d -m 700 -o "$qh_uid" -g "$qh_gid" "/mnt/home/$qh_user/.ssh"; '
            'sudo install -m 600 -o "$qh_uid" -g "$qh_gid" "$qh_auth_src" '
            '"/mnt/home/$qh_user/.ssh/authorized_keys"',
            "sudo chroot /mnt sh -c "
            + self.shell_quote(
                "set -e; "
                ": > /etc/sudoers.d/99-quickhatch-nopasswd; "
                "getent group wheel >/dev/null 2>&1 && "
                "printf '%s\\n' '%wheel ALL=(ALL:ALL) NOPASSWD: ALL' "
                ">> /etc/sudoers.d/99-quickhatch-nopasswd; "
                "getent group sudo >/dev/null 2>&1 && "
                "printf '%s\\n' '%sudo ALL=(ALL:ALL) NOPASSWD: ALL' "
                ">> /etc/sudoers.d/99-quickhatch-nopasswd; "
                "test -s /etc/sudoers.d/99-quickhatch-nopasswd"
            ),
            "sudo chmod 440 /mnt/etc/sudoers.d/99-quickhatch-nopasswd",
        ]

    def configure_network(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts, profile
        return [
            "sudo mkdir -p /mnt/etc/NetworkManager/conf.d",
            "printf '%s\\n' '[keyfile]' 'unmanaged-devices=none' | "
            "sudo tee /mnt/etc/NetworkManager/conf.d/10-quickhatch-managed.conf >/dev/null",
        ]

    def install_bootloader(self, facts: SetupFacts, profile: UserProfile) -> list[str]:
        del facts, profile
        return []

    def pre_reboot_checks(self, facts: SetupFacts) -> list[str]:
        del facts
        return [
            "test -s /mnt/etc/fstab",
            "find /mnt/boot -maxdepth 2 -type f \\( -name 'vmlinuz*' -o -name 'initramfs*' -o -name 'initrd*' \\) | grep -q .",
            "find /mnt/boot -maxdepth 4 -type f \\( -iname 'grub*.efi' -o -iname 'bootx64.efi' \\) | grep -q .",
            "find /mnt/home -maxdepth 3 -path '*/.ssh/authorized_keys' -size +0c | grep -q .",
            "test -s /mnt/etc/sudoers.d/99-quickhatch-nopasswd",
        ]

    def post_reboot_package_query(self, packages: list[str], facts: SetupFacts) -> str:
        del facts
        return " ".join(packages)

    def service_query(self, services: list[str], facts: SetupFacts) -> str:
        del facts
        return "systemctl is-enabled " + " ".join(services)

    def final_boot_audit(self, facts: SetupFacts) -> list[str]:
        del facts
        return [
            "test -s /etc/fstab",
            "find /boot -maxdepth 3 -type f \\( -name 'vmlinuz*' -o -name 'initramfs*' -o -name 'initrd*' \\) | grep -q .",
            "systemctl is-system-running --wait || true",
        ]

    def render_operation(
        self, operation: SetupOperation, facts: SetupFacts, profile: UserProfile
    ) -> list[str]:
        del facts
        sudo = "sudo "
        target = shlex.quote(operation.target)
        mountpoint = shlex.quote(operation.mountpoint)
        filesystem = (operation.filesystem or "ext4").lower()
        if operation.kind == "partition_disk":
            return [
                f"{sudo}parted -s {target} mklabel gpt "
                "mkpart ESP fat32 1MiB 513MiB set 1 esp on "
                "mkpart primary ext4 513MiB 100%"
            ]
        if operation.kind == "format_partition":
            if filesystem == "btrfs":
                return [f"{sudo}mkfs.btrfs -f {target}"]
            if filesystem in {"fat32", "vfat"}:
                return [f"{sudo}mkfs.fat -F32 {target}"]
            return [f"{sudo}mkfs.ext4 -F {target}"]
        if operation.kind == "mount_partition":
            return [f"{sudo}mkdir -p {mountpoint} && {sudo}mount {target} {mountpoint}"]
        if operation.kind == "write_fstab":
            root = shlex.quote(str(operation.options.get("root", "/mnt")))
            return [f"{sudo}genfstab -U {root} | {sudo}tee {root}/etc/fstab >/dev/null"]
        if operation.kind == "install_bootloader":
            efi_dir = str(operation.options.get("efi_directory", self.efi_mountpoint()))
            if efi_dir == "/mnt":
                efi_dir = "/"
            elif efi_dir.startswith("/mnt/"):
                efi_dir = efi_dir[4:]
            efi = shlex.quote(efi_dir)
            return [
                f"{sudo}chroot /mnt grub-install --target=x86_64-efi "
                f"--efi-directory={efi} --bootloader-id=quickhatch --recheck",
                f"{sudo}chroot /mnt grub-mkconfig -o /boot/grub/grub.cfg",
            ]
        if operation.kind == "create_user":
            user = self.target_user(profile)
            return [
                f"{sudo}chroot /mnt id {shlex.quote(user)} >/dev/null 2>&1 || {sudo}chroot /mnt useradd -m -s /bin/bash {shlex.quote(user)}"
            ]
        if operation.kind == "enable_network_reconnect":
            return [f"{sudo}chroot /mnt systemctl enable NetworkManager ssh sshd || true"]
        if operation.kind == "install_ssh_key":
            user = self.target_user(profile)
            return [
                f"{sudo}install -d -m 700 /mnt/home/{shlex.quote(user)}/.ssh",
                f"test -s /root/.ssh/authorized_keys && {sudo}install -m 600 /root/.ssh/authorized_keys /mnt/home/{shlex.quote(user)}/.ssh/authorized_keys",
            ]
        if operation.kind == "controlled_reboot":
            return [f"{sudo}reboot"]
        if operation.kind == "network_security_change":
            command = str(operation.options.get("command", "")).strip()
            return [command] if command else []
        if operation.kind == "package_db_repair":
            command = str(operation.options.get("command", "")).strip()
            return [command] if command else []
        raise ValueError(f"Unsupported setup operation: {operation.kind}")

    def build_install_spine(self, facts: SetupFacts, profile: UserProfile) -> InstallSpine:
        commands = [
            *self.prepare_disk(facts, profile),
            *self.install_base_system(facts, profile),
            *self.configure_user_ssh_sudo(facts, profile),
            *self.configure_network(facts, profile),
            *self.install_bootloader(facts, profile),
        ]
        return InstallSpine(
            family=self.family,
            adapter_version=self.version,
            commands=commands,
            verification=self.pre_reboot_checks(facts),
        )
