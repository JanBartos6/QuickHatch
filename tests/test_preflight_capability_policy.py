from __future__ import annotations

import shlex

from quickhatch.setup.capabilities import CapabilityStore
from quickhatch.setup.preflight import preflight_setup_command, preflight_setup_decisions
from quickhatch.setup.types import PreflightDecisionKind, SetupFacts

RUN_ID = "policy-run"
PROFILE = "full_disk_wipe"


def _facts(
    *,
    family: str = "generic",
    package_manager: str = "unknown",
    is_live: bool = False,
    is_root: bool = False,
    install_profile: str = "configure_existing",
) -> SetupFacts:
    return SetupFacts(
        distro_family="unknown" if family == "generic" else family,
        package_manager=package_manager,
        community_helper="yay" if family == "arch" else "none",
        init_system="systemd",
        current_user="root" if is_root else "quickhatch",
        login_user="root" if is_root else "quickhatch",
        is_root=is_root,
        is_live=is_live,
        sudo_nopasswd=True,
        raw={"install_profile": install_profile},
    )


def _configure_existing_facts(
    *, family: str = "generic", package_manager: str = "unknown"
) -> SetupFacts:
    return _facts(
        family=family,
        package_manager=package_manager,
        is_live=False,
        is_root=False,
        install_profile="configure_existing",
    )


def _live_root_facts(*, family: str = "generic", package_manager: str = "unknown") -> SetupFacts:
    return _facts(
        family=family,
        package_manager=package_manager,
        is_live=True,
        is_root=True,
        install_profile="full_disk_wipe",
    )


def _block_reasons(command: str, facts: SetupFacts) -> list[str]:
    return [
        decision.reason
        for decision in preflight_setup_decisions(command, facts)
        if decision.kind == PreflightDecisionKind.BLOCK
    ]


def _assert_no_hard_block(command: str, facts: SetupFacts) -> None:
    blocks = _block_reasons(command, facts)
    assert not blocks, f"{command} unexpectedly hard-blocked: {blocks}"


def _assert_hard_blocks(command: str, facts: SetupFacts) -> None:
    assert _block_reasons(command, facts), f"{command} unexpectedly passed"


def _grant(
    *,
    kind: str,
    target: str = "",
    run_id: str = RUN_ID,
    install_profile: str = PROFILE,
    ttl_seconds: float = 7200,
    one_time_use: bool = False,
) -> tuple[CapabilityStore, str]:
    store = CapabilityStore(run_id=run_id, install_profile=install_profile)
    grant = store.grant(
        kind=kind,
        target=target,
        confirmation_text=f"CONFIRM {kind} {target}".strip(),
        confirmation_id="preview-policy",
        reason="policy regression grant",
        source="harness_generated",
        one_time_use=one_time_use,
        ttl_seconds=ttl_seconds,
        preview_id="preview-policy",
    )
    return store, grant.id


def _assert_allowed_with_capability(command: str, *, kind: str, target: str = "") -> None:
    store, _ = _grant(kind=kind, target=target)

    error = preflight_setup_command(
        command,
        _facts(is_live=True, is_root=True, install_profile=PROFILE),
        capability_store=store,
    )

    assert error is None


def _assert_blocks_with_capability(command: str, store: CapabilityStore) -> None:
    error = preflight_setup_command(
        command,
        _facts(is_live=True, is_root=True, install_profile=PROFILE),
        capability_store=store,
    )

    assert error, command


def _wrapped_variants(command: str) -> list[str]:
    quoted = shlex.quote(command)
    return [
        command,
        f"sudo {command}",
        f"bash -c {quoted}",
        f"sudo sh -c {quoted}",
        f"true; {command}",
    ]


WRAPPED_DESTRUCTIVE_VARIANTS = [
    variant
    for command in (
        "mkfs.ext4 /dev/sda1",
        "parted /dev/sda mklabel gpt",
        "wipefs -a /dev/sda",
    )
    for variant in _wrapped_variants(command)
] + [
    'TARGET=/dev/sda1; mkfs.ext4 "$TARGET"',
    "printf 'label: gpt\\n' | sudo sfdisk /dev/sda",
]


def test_safe_wrong_package_commands_do_not_hard_block() -> None:
    cases = (
        (
            "apt install definitely-not-real-package",
            _configure_existing_facts(family="ubuntu", package_manager="apt"),
        ),
        (
            "dnf install definitely-not-real-package",
            _configure_existing_facts(family="fedora", package_manager="dnf"),
        ),
        (
            "pacman -S definitely-not-real-package",
            _configure_existing_facts(family="arch", package_manager="pacman"),
        ),
        ("apt install vlc", _configure_existing_facts(family="fedora", package_manager="dnf")),
        (
            "dnf install --assumeno definitely-not-real-package",
            _configure_existing_facts(family="fedora", package_manager="dnf"),
        ),
        (
            "apt-get install --simulate definitely-not-real-package",
            _configure_existing_facts(family="ubuntu", package_manager="apt"),
        ),
    )

    for command, facts in cases:
        _assert_no_hard_block(command, facts)


def test_search_and_diagnostic_commands_do_not_hard_block() -> None:
    facts = _configure_existing_facts(family="ubuntu", package_manager="apt")
    commands = (
        "apt search vlc",
        "dnf search vlc",
        "pacman -Ss vlc",
        "flatpak search spotify",
        "snap find spotify",
        "lsblk",
        "findmnt",
        "blkid",
        "efibootmgr -v",
        "systemctl status sshd",
        "ip route show",
        "nmcli device status",
    )

    for command in commands:
        _assert_no_hard_block(command, facts)


def test_destructive_disk_commands_block_without_capability() -> None:
    facts = _live_root_facts()
    commands = (
        "mkfs.ext4 /dev/sda1",
        "mkswap /dev/sda2",
        "wipefs -a /dev/sda",
        "dd if=/dev/zero of=/dev/sda",
        "shred /dev/sda",
        "parted /dev/sda mklabel gpt",
        "fdisk /dev/sda",
        "cfdisk /dev/sda",
        "gdisk /dev/sda",
    )

    for command in commands:
        _assert_hard_blocks(command, facts)


def test_wrapped_destructive_commands_block_without_capability() -> None:
    facts = _live_root_facts()

    for command in WRAPPED_DESTRUCTIVE_VARIANTS:
        _assert_hard_blocks(command, facts)


def test_matching_capability_allows_exact_target() -> None:
    capability_cases = (
        ("mkfs.ext4 /dev/sda1", "format_filesystem", "/dev/sda1"),
        ("parted /dev/sda mklabel gpt", "disk_partition", "/dev/sda"),
        (
            "printf 'UUID=x / ext4 defaults 0 1\\n' | sudo tee /mnt/etc/fstab",
            "write_fstab",
            "/target-root/etc/fstab",
        ),
        ("systemctl enable --now firewalld", "network_security_change", ""),
    )

    for command, kind, target in capability_cases:
        _assert_allowed_with_capability(command, kind=kind, target=target)


def test_mismatched_capability_blocks() -> None:
    mismatch_cases = (
        ("mkfs.ext4 /dev/sda2", "format_filesystem", "/dev/sda1"),
        ("parted /dev/sdb mklabel gpt", "disk_partition", "/dev/sda"),
        ("mkfs.ext4 /dev/sda1", "disk_partition", "/dev/sda1"),
        ("parted /dev/sda mklabel gpt", "format_filesystem", "/dev/sda"),
        ("grub-install /dev/sda", "write_fstab", "/target-root/etc/fstab"),
    )
    for command, kind, target in mismatch_cases:
        store, _ = _grant(kind=kind, target=target)
        _assert_blocks_with_capability(command, store)

    payload_store, grant_id = _grant(kind="format_filesystem", target="/dev/sda1")
    wrong_run = CapabilityStore.from_payload(
        payload_store.to_payload(),
        run_id=f"{RUN_ID}-wrong",
        install_profile=PROFILE,
    )
    _assert_blocks_with_capability("mkfs.ext4 /dev/sda1", wrong_run)

    wrong_profile = CapabilityStore.from_payload(
        payload_store.to_payload(),
        run_id=RUN_ID,
        install_profile="configure_existing",
    )
    _assert_blocks_with_capability("mkfs.ext4 /dev/sda1", wrong_profile)

    expired, _ = _grant(kind="format_filesystem", target="/dev/sda1", ttl_seconds=-1)
    _assert_blocks_with_capability("mkfs.ext4 /dev/sda1", expired)

    consumed, consumed_id = _grant(
        kind="format_filesystem",
        target="/dev/sda1",
        one_time_use=True,
    )
    consumed.consume(consumed_id)
    _assert_blocks_with_capability("mkfs.ext4 /dev/sda1", consumed)

    payload_store.consume(grant_id)


def test_configure_existing_blocks_install_spine_mutations() -> None:
    facts = _configure_existing_facts(family="fedora", package_manager="dnf")
    commands = (
        "pacstrap -K /mnt base linux",
        "debootstrap stable /mnt",
        "dnf --installroot=/mnt install kernel",
        "sudo mount /dev/sda2 /mnt",
        "printf 'UUID=x / ext4 defaults 0 1\\n' | sudo tee /mnt/etc/fstab",
        "grub-install /dev/sda",
        "parted /dev/sda mklabel gpt",
        "mkfs.ext4 /dev/sda1",
    )

    for command in commands:
        _assert_hard_blocks(command, facts)


def test_network_security_activation_blocks_without_consent() -> None:
    facts = _configure_existing_facts(family="fedora", package_manager="dnf")
    blocked_commands = (
        "ufw enable",
        "systemctl enable --now firewalld",
        "systemctl enable --now mullvad-daemon",
        "ip route del default",
        "ip link set eth0 down",
    )
    allowed_installs = (
        "apt install mullvad-vpn",
        "dnf install firewalld",
        "pacman -S ufw",
    )

    for command in blocked_commands:
        _assert_hard_blocks(command, facts)
    for command in allowed_installs:
        _assert_no_hard_block(command, facts)


def test_readonly_network_status_commands_allowed() -> None:
    facts = _configure_existing_facts(family="ubuntu", package_manager="apt")
    commands = (
        "ip addr",
        "ip route show",
        "resolvectl status",
        "nmcli device status",
        "systemctl status sshd",
        "journalctl -u sshd --no-pager",
    )

    for command in commands:
        _assert_no_hard_block(command, facts)
