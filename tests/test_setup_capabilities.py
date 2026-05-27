from __future__ import annotations

from quickhatch.setup.capabilities import (
    CapabilityStore,
    build_operation_preview,
    canonicalize_target,
    dangerous_command_requirements,
)
from quickhatch.setup.operations import SetupOperation
from quickhatch.setup.preflight import preflight_setup_command
from quickhatch.setup.types import SetupFacts


def _store_with_format_capability(target: str = "/dev/sda1") -> CapabilityStore:
    store = CapabilityStore(run_id="run-1", install_profile="full_disk_wipe")
    store.grant(
        kind="format_filesystem",
        target=target,
        confirmation_text=f"FORMAT {target}",
        confirmation_id="preview-1",
        reason="test grant",
        preview_id="preview-1",
    )
    return store


def _store_with_capability(kind: str, target: str = "") -> CapabilityStore:
    store = CapabilityStore(run_id="run-1", install_profile="full_disk_wipe")
    store.grant(
        kind=kind,
        target=target,
        confirmation_text=f"CONFIRM {kind} {target}".strip(),
        confirmation_id="preview-1",
        reason="test grant",
        source="harness_generated",
        one_time_use=False,
        preview_id="preview-1",
    )
    return store


def test_raw_mkfs_blocks_without_capability() -> None:
    facts = SetupFacts(is_live=True, is_root=True)

    error = preflight_setup_command("mkfs.ext4 /dev/sda1", facts)

    assert error
    assert "explicit QuickHatch capability" in error


def test_raw_mkfs_passes_with_matching_capability() -> None:
    facts = SetupFacts(is_live=True, is_root=True)
    store = _store_with_format_capability()

    error = preflight_setup_command(
        "mkfs.ext4 /dev/sda1",
        facts,
        capability_store=store,
    )

    assert error is None


def test_consumed_capability_cannot_be_reused_after_reload() -> None:
    store = _store_with_format_capability()
    grant = store.allows(kind="format_filesystem", target="/dev/sda1")
    assert grant is not None
    store.consume(grant.id)

    reloaded = CapabilityStore.from_payload(
        store.to_payload(),
        run_id="run-1",
        install_profile="full_disk_wipe",
    )

    assert reloaded.allows(kind="format_filesystem", target="/dev/sda1") is None


def test_capability_from_wrong_run_id_is_ignored() -> None:
    store = _store_with_format_capability()

    reloaded = CapabilityStore.from_payload(
        store.to_payload(),
        run_id="run-2",
        install_profile="full_disk_wipe",
    )

    assert reloaded.allows(kind="format_filesystem", target="/dev/sda1") is None


def test_capability_target_mismatch_blocks() -> None:
    facts = SetupFacts(is_live=True, is_root=True)
    store = _store_with_format_capability("/dev/nvme1n1p1")

    error = preflight_setup_command(
        "mkfs.ext4 /dev/nvme0n1p1",
        facts,
        capability_store=store,
    )

    assert error
    assert "explicit QuickHatch capability" in error


def test_stale_disk_inventory_requires_reconfirmation() -> None:
    store = CapabilityStore(run_id="run-1", install_profile="full_disk_wipe")
    inventory_a = "sda 512G Samsung disk ext4 root uuid-a /mnt"
    inventory_b = "sda 512G Samsung disk ext4 root uuid-b /mnt"
    store.grant(
        kind="format_filesystem",
        target="/dev/sda1",
        confirmation_text="FORMAT /dev/sda1",
        confirmation_id="preview-1",
        reason="test grant",
        disk_inventory=inventory_a,
        preview_id="preview-1",
    )

    assert (
        store.allows(
            kind="format_filesystem",
            target="/dev/sda1",
            disk_inventory=inventory_b,
        )
        is None
    )


def test_wrapped_destructive_shell_is_detected() -> None:
    facts = SetupFacts(is_live=True, is_root=True)
    commands = (
        "bash -c 'mkfs.ext4 /dev/sda1'",
        'sudo sh -c "parted -s /dev/sda mklabel gpt"',
        "alias wipe='mkfs.ext4 /dev/sda1'; wipe",
        "echo ok; mkfs.ext4 /dev/sda1",
        "echo $(mkfs.ext4 /dev/sda1)",
    )

    for command in commands:
        error = preflight_setup_command(command, facts)
        assert error, command
        assert "explicit QuickHatch capability" in error


def test_broad_high_blast_radius_shell_requires_capability() -> None:
    facts = SetupFacts(is_live=True, is_root=True)
    commands = (
        "cryptsetup luksFormat /dev/sda2",
        "dd if=/dev/zero of=/dev/sda bs=1M count=10",
        "dd if=/dev/zero of=$(echo /dev/sda) bs=1M count=10",
        "mount /dev/sda2 /mnt",
        "blkdiscard /dev/sda",
        "pvcreate /dev/sda2",
        "mkswap /dev/sda3",
        "shred -n1 /dev/sda",
        "cryptsetup erase /dev/sda2",
        "cryptsetup luksErase /dev/sda2",
        "parted -s /dev/sda set 1 boot on",
        "printf ',,L,*\\n' | sfdisk /dev/sda",
        "efibootmgr -c -d /dev/sda -p 1",
        "efibootmgr -b 0003 -B",
        "efibootmgr -o 0003,0001",
        "sudo install -m 644 /tmp/fstab /mnt/etc/fstab",
        "sudo cp /tmp/fstab /target/etc/fstab",
        "sudo sed -i 's/defaults/noatime/' /mnt/etc/fstab",
        "sudo truncate -s 0 /etc/fstab",
        "python3 -c \"open('/mnt/etc/fstab','w').write('bad')\"",
        "sudo rm -rf /var/lib/dpkg/updates",
        "sudo rm -rf /var/lib/pacman/db.lck",
        "sudo find /var/lib/dpkg -name lock -delete",
        "sudo find /var/lib/pacman -name db.lck -exec rm -f {} +",
        "sudo truncate -s 0 /var/lib/dpkg/status",
        "sudo sed -i '1d' /var/lib/pacman/local/foo/desc",
        "sudo python3 -c \"open('/var/lib/rpm/Packages','w').write('')\"",
        "sudo pacman -D --asdeps nodejs",
        "sudo pacman -S --dbonly fake-package",
        "sudo grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=quickhatch",
        "sudo bootctl install",
        "systemctl enable --now ufw",
        "sudo ufw allow OpenSSH",
        "sudo ufw default deny outgoing",
        "ip6tables -P INPUT DROP",
        "rm -rf /mnt",
        "rm -rf /mnt/*",
        "sudo telinit 6",
        "sudo shutdown now",
        "sudo systemctl --force reboot",
        "sudo kexec -e",
    )

    for command in commands:
        error = preflight_setup_command(command, facts)
        assert error, command
        assert "explicit QuickHatch capability" in error


def test_uuid_label_mounts_to_target_root_require_capability() -> None:
    facts = SetupFacts(is_live=True, is_root=True)
    commands = (
        "mount UUID=1111-2222 /mnt",
        "mount LABEL=rootfs /target",
        "mount -U 1111-2222 /mnt/sysroot",
        "mount -L rootfs /mnt/target",
    )

    for command in commands:
        error = preflight_setup_command(command, facts)
        assert error, command
        assert "explicit QuickHatch capability" in error


def test_variable_expanded_high_blast_radius_shell_requires_capability() -> None:
    facts = SetupFacts(is_live=True, is_root=True)
    commands = (
        'TARGET=/dev/sda1; mkfs.ext4 -F "$TARGET"',
        'DISK=/dev/sda; ROOT=${DISK}1; mkfs.ext4 -F "$ROOT"',
        'ROOT=/dev/sda1; mount "$ROOT" /mnt',
        'DISK=/dev/sda; dd if=/dev/zero of="$DISK" bs=1M count=1',
    )

    for command in commands:
        error = preflight_setup_command(command, facts)
        assert error, command
        assert "explicit QuickHatch capability" in error

    requirements = dangerous_command_requirements(
        'DISK=/dev/sda; ROOT=${DISK}1; mkfs.ext4 -F "$ROOT"'
    )
    assert any(
        item["kind"] == "format_filesystem" and item["target"] == "/dev/sda1"
        for item in requirements
    )


def test_confirmed_mount_target_capability_allows_target_root_mount() -> None:
    facts = SetupFacts(is_live=True, is_root=True)
    store = CapabilityStore(run_id="run-1", install_profile="full_disk_wipe")
    store.grant(
        kind="mount_target",
        target="/mnt",
        confirmation_text="MOUNT /mnt",
        confirmation_id="preview-1",
        reason="test mount grant",
        source="harness_generated",
        one_time_use=False,
        preview_id="preview-1",
    )

    error = preflight_setup_command(
        "sudo mount /dev/sda2 /mnt",
        facts,
        capability_store=store,
    )

    assert error is None


def test_confirmed_tier1_capabilities_allow_harness_generated_operations() -> None:
    facts = SetupFacts(is_live=True, is_root=True)
    cases = (
        (
            "write_fstab",
            "/target-root/etc/fstab",
            "sudo genfstab -U /mnt | sudo tee -a /mnt/etc/fstab >/dev/null",
        ),
        (
            "bootloader_install",
            "/boot",
            "sudo arch-chroot /mnt grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=quickhatch",
        ),
        (
            "controlled_reboot",
            "",
            "sudo reboot",
        ),
        (
            "package_db_repair",
            "/var/lib",
            "sudo pacman -D --asdeps nodejs",
        ),
    )

    for kind, target, command in cases:
        error = preflight_setup_command(
            command,
            facts,
            capability_store=_store_with_capability(kind, target),
        )
        assert error is None, command


def test_controlled_reboot_capability_does_not_mask_other_ssh_lockouts() -> None:
    facts = SetupFacts(is_live=True, is_root=True)
    error = preflight_setup_command(
        "sudo reboot; sudo systemctl stop sshd",
        facts,
        capability_store=_store_with_capability("controlled_reboot"),
    )

    assert error
    assert "cut off remote access" in error


def test_ambiguous_variable_high_blast_radius_shell_requires_capability() -> None:
    facts = SetupFacts(is_live=True, is_root=True)
    commands = (
        'mkfs.ext4 -F "$ROOT"',
        'mount "$ROOT" /mnt',
        'dd if=/dev/zero of="$DISK" bs=1M count=1',
    )

    for command in commands:
        error = preflight_setup_command(command, facts)
        assert error, command
        assert "explicit QuickHatch capability" in error


def test_mapper_luks_lvm_fdisk_and_efi_mutations_require_capability() -> None:
    facts = SetupFacts(is_live=True, is_root=True)
    commands = (
        "mkfs.ext4 /dev/mapper/cryptroot",
        "mount /dev/mapper/cryptroot /mnt",
        "cryptsetup open /dev/sda2 cryptroot",
        "vgcreate vg0 /dev/sda2",
        "lvcreate -L 20G -n root vg0",
        "lvremove -y /dev/vg0/root",
        "vgremove -y vg0",
        "pvremove -y /dev/sda2",
        "parted -s /dev/sda rm 1",
        "printf 'd\\nw\\n' | fdisk /dev/sda",
        "fdisk /dev/sda <<EOF\np\nw\nEOF",
        "efibootmgr -b 0003 -B",
        "efibootmgr -o 0003,0001",
    )

    for command in commands:
        error = preflight_setup_command(command, facts)
        assert error, command
        assert "explicit QuickHatch capability" in error


def test_dangerous_operation_preview_is_required_for_grants() -> None:
    operation = SetupOperation(kind="format_partition", target="/dev/sda1", filesystem="ext4")
    preview = build_operation_preview(
        operation=operation,
        rendered_commands=["sudo mkfs.ext4 -F /dev/sda1"],
        install_profile="full_disk_wipe",
        canonical_target="/dev/sda1",
        disk_inventory="sda1 512M part",
    )

    assert preview.confirmation_token == "FORMAT /dev/sda1"
    assert preview.erases_data is True

    store = CapabilityStore(run_id="run-1", install_profile="full_disk_wipe")
    try:
        store.grant(
            kind="format_filesystem",
            target="/dev/sda1",
            confirmation_text=preview.confirmation_token,
            confirmation_id=preview.preview_id,
            reason="test",
            preview_id="",
        )
    except ValueError as exc:
        assert "preview id" in str(exc)
    else:
        raise AssertionError("grant without preview id should fail")


def test_canonical_target_aliases_and_device_mismatch() -> None:
    assert canonicalize_target("/mnt") == "/target-root"
    assert canonicalize_target("/target") == "/target-root"
    assert canonicalize_target("/dev/nvme0n1p1") != canonicalize_target("/dev/nvme1n1p1")
    assert canonicalize_target("/dev/disk/by-id/nvme-Samsung-part1").startswith("/dev/disk/by-id/")


def test_dangerous_command_requirements_classify_network_security() -> None:
    requirements = dangerous_command_requirements("sudo systemctl enable --now mullvad-daemon")

    assert any(item["kind"] == "network_security_change" for item in requirements)
