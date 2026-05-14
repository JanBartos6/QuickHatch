"""Step 8: Export migration bundle."""

from __future__ import annotations

import json
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

import questionary
import yaml

from quickhatch.ai.instruction_gen import save_instructions
from quickhatch.config import UserProfile
from quickhatch.console import console, print_info, print_step, print_success

GENERATED_EXPORT_FILES = {
    "MIGRATION_INSTRUCTIONS.md",
    "START_HERE.md",
    "quickhatch-profile.json",
    "quickhatch-profile.yaml",
    "quickhatch-handoff.json",
    "quickstart.sh",
    "install.sh",
    "quickhatch-migration-bundle.zip",
    ".api_key",
}

HANDOFF_FILE_NAME = "quickhatch-handoff.json"


def redacted_profile_dict(profile: UserProfile) -> dict[str, Any]:
    """Return profile JSON safe to copy between machines."""
    profile_dict = asdict(profile)
    if isinstance(profile_dict.get("ai_config"), dict):
        profile_dict["ai_config"]["api_key"] = None
    return profile_dict


def build_handoff_payload(
    profile: UserProfile,
    *,
    suggestions: list[dict[str, Any]] | None = None,
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Portable checkpoint for moving the finished plan to another PC.

    Deliberately excludes API keys and SSH private keys. The receiving
    machine should generate/select its own SSH key and re-export the bundle
    before starting Remote Setup.
    """
    import time

    return {
        "type": "quickhatch-handoff",
        "version": 1,
        "created_at": time.time(),
        "profile": redacted_profile_dict(profile),
        "bridge": {
            "suggestions": list(suggestions or []),
            "messages": list(messages or []),
        },
        "target_step": "setup",
        "notes": (
            "Load this in QuickHatch on another helper PC to reuse the saved "
            "profile and analysis plan. API keys and SSH private keys are not "
            "included; generate or select a local SSH key on the receiving PC "
            "and re-export the bundle before Remote Setup."
        ),
    }


def _write_unix_text(path: Path, text: str) -> None:
    """Write a Linux shell-friendly text file with LF line endings."""
    path.write_text(text, encoding="utf-8", newline="\n")


def _boot_menu_hint(distro: str) -> str:
    distro_lower = (distro or "").lower()
    if "arch" in distro_lower:
        return "At the boot menu, use the default Arch boot entry to land in the live shell."
    if "ubuntu" in distro_lower or "debian" in distro_lower or "fedora" in distro_lower:
        return "If the ISO offers it, choose the Try/Live option instead of Install."
    return "Boot into the live environment instead of starting the graphical installer immediately."


def generate_start_here(profile: UserProfile, *, include_install_script: bool) -> str:
    """Generate the practical operator guide shipped in the export bundle."""
    distro = profile.preferences.distro_preference or "your chosen distro"
    offline_mode = (profile.preferences.another_machine or "").strip().lower() == "no"
    quickstart_block = _quickstart_usb_finder_block()
    simple_quickstart_block = _quickstart_simple_mount_block()

    lines = [
        "# QuickHatch Start Here",
        "",
        "This file is the practical handoff for the exported bundle.",
        "Use it together with `MIGRATION_INSTRUCTIONS.md` and the shipped scripts.",
        "",
        "## Before You Boot",
        "",
        f"- Download the official ISO for {distro}.",
        "- Write it to a USB drive with Rufus, Ventoy, or balenaEtcher.",
        "- Copy this export bundle somewhere you can access from the target machine.",
        "  A second USB drive is the simplest option.",
        "",
        "## Boot The Target Machine",
        "",
        "- Boot from the USB drive.",
        f"- {_boot_menu_hint(distro)}",
        "- Get networking online before you continue.",
        "  On Arch-based live environments, `iwctl` or `nmcli` are the usual tools.",
        "",
        "## Transfer The Bundle",
        "",
        "- If you brought a second USB drive, plug it in and copy/extract the bundle locally.",
        "- Network copy is also fine, but do not rely on `scp` until `quickstart.sh` has enabled SSH.",
        "",
    ]

    if offline_mode:
        lines.extend(
            [
                "## Single-Machine Path",
                "",
                "You told QuickHatch you do not have a second machine available during install.",
                "That means there is no live AI agent during the installation itself.",
                "",
            ]
        )
        if include_install_script:
            lines.extend(
                [
                    "1. Extract the bundle in the live environment.",
                    "2. Review `install.sh` if you want to audit it first.",
                    "3. Run `sudo bash install.sh`.",
                    "4. Re-type the target disk when the script asks for destructive confirmation.",
                    "5. Let the machine reboot. First-boot configuration continues automatically.",
                    "6. If something fails, inspect `/var/log/quickhatch-firstboot.log` on the installed system.",
                ]
            )
        else:
            lines.extend(
                [
                    "This bundle does not contain `install.sh`.",
                    "Generate the bundle from the web GUI after analysis if you want the offline single-machine script path.",
                ]
            )
    else:
        lines.extend(
            [
                "## Live Agent Path",
                "",
                "1. Copy the whole QuickHatch export folder to a second USB drive.",
                "2. Boot the target ISO and get online.",
                "3. Plug in the bundle USB. If it appears as `/dev/sdb1`, use the simple path:",
                "",
                "```sh",
                simple_quickstart_block,
                "```",
                "",
                "Use the path printed by `find` if your bundle folder name differs.",
                "If `/dev/sdb1` is not the bundle USB, run `lsblk -f` and mount the correct partition instead.",
                "",
                "Finder fallback:",
                "",
                "```sh",
                quickstart_block,
                "```",
                "",
                "4. Note the IP address and username it prints.",
                "5. Return to QuickHatch on Windows, enter those values in Remote Setup, test the connection, then start the agent.",
                "6. Leave the target machine alone unless the agent explicitly asks you to intervene.",
                "",
                "If you prefer the distro installer, that also works:",
                "- Install Linux manually first.",
                "- Boot into the installed system.",
                "- Run `bash quickstart.sh` there.",
                "- Start Remote Setup from Windows; the agent will skip the fresh-install phase.",
            ]
        )

    return "\n".join(lines) + "\n"


def _quickstart_simple_mount_block() -> str:
    return """mkdir -p /mnt/usb
mount /dev/sdb1 /mnt/usb
QH_START=$(find /mnt/usb -name quickstart.sh -print -quit)
[ -n "$QH_START" ] || { echo "quickstart.sh not found on /mnt/usb"; exit 1; }
bash "$QH_START"
"""


def _quickstart_usb_finder_block() -> str:
    """Return the one-paste target command block for finding the bundle USB.

    The block is intentionally POSIX-sh compatible and mounts candidate
    partitions read-only. If it finds the bundle, `quickstart.sh` resolves
    its own directory and runs in place.
    """
    return """SUDO=
[ "$(id -u)" = 0 ] || SUDO=sudo
QH_MNT=/mnt/quickhatch-bundle

qh_bundle_here() {
  [ -f "$1/quickstart.sh" ] && {
    [ -f "$1/quickhatch-profile.json" ] ||
    [ -f "$1/quickhatch-handoff.json" ]
  }
}

for d in . "$PWD" /run/media/*/* /media/*/* /mnt/* \\
  /tmp/quickhatch /var/tmp/quickhatch; do
  if qh_bundle_here "$d"; then
    bash "$d/quickstart.sh"; exit $?
  fi
done

$SUDO mkdir -p "$QH_MNT"
for p in $(lsblk -rpno NAME,TYPE | awk '$2=="part"{print $1}'); do
  $SUDO mount -o ro "$p" "$QH_MNT" 2>/dev/null || continue
  if qh_bundle_here "$QH_MNT"; then
    bash "$QH_MNT/quickstart.sh"; exit $?
  fi
  $SUDO umount "$QH_MNT" 2>/dev/null || true
done
echo "QuickHatch bundle not found. Check the bundle USB with: lsblk -f"
exit 1"""


def run(profile: UserProfile) -> None:
    print_step(8, "Export Migration Bundle")

    console.print(
        "We'll now export everything into a migration bundle that you can\n"
        "transfer to your new Linux system.\n"
    )

    # Choose output location
    default_dir = str(Path.home() / "QuickHatch-Migration")
    output_dir_str = questionary.text(
        "Where should we save the migration bundle?",
        default=default_dir,
    ).ask()
    if output_dir_str is None:
        raise KeyboardInterrupt

    output_dir = Path(output_dir_str)
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale in GENERATED_EXPORT_FILES:
        path = output_dir / stale
        if path.exists():
            path.unlink()
    for stale_pub in output_dir.glob("*.pub"):
        stale_pub.unlink()

    # Generate instruction file
    print_info("Generating AI instruction file...")
    inst_path = save_instructions(profile, output_dir)
    print_success(f"Instructions saved: {inst_path}")

    # Save profile as JSON
    print_info("Saving profile data...")
    profile_path = output_dir / "quickhatch-profile.json"
    # Exclude sensitive data like API keys from the JSON dump
    profile_dict = redacted_profile_dict(profile)
    profile_path.write_text(json.dumps(profile_dict, indent=2))

    # Save as YAML for human readability
    yaml_path = output_dir / "quickhatch-profile.yaml"
    yaml_path.write_text(yaml.dump(profile_dict, default_flow_style=False, sort_keys=False))

    handoff_path = output_dir / HANDOFF_FILE_NAME
    handoff_path.write_text(
        json.dumps(build_handoff_payload(profile), indent=2),
        encoding="utf-8",
    )
    print_info(f"Portable handoff saved: {handoff_path}")

    start_here_path = output_dir / "START_HERE.md"
    start_here_path.write_text(
        generate_start_here(profile, include_install_script=False),
        encoding="utf-8",
    )

    # Store API key separately if present (with restricted permissions)
    if profile.ai_config.api_key:
        api_key_path = output_dir / ".api_key"
        api_key_path.write_text(profile.ai_config.api_key)
        try:
            api_key_path.chmod(0o600)
        except OSError:
            pass  # Windows may not support chmod
        print_info("API key saved to .api_key (owner-only permissions)")

    # Copy SSH public key if available
    if profile.ssh_public_key_path:
        ssh_pub = Path(profile.ssh_public_key_path)
        if ssh_pub.exists():
            dest = output_dir / ssh_pub.name
            dest.write_text(ssh_pub.read_text())
            print_info(f"SSH public key copied: {dest}")

    # Generate a quick-start script
    quickstart_path = output_dir / "quickstart.sh"
    _write_unix_text(quickstart_path, _generate_quickstart(profile))
    print_info(f"Quick-start script saved: {quickstart_path}")

    # Create ZIP bundle. Explicitly skip dotfiles — the API key is saved
    # as .api_key for local use only; shipping it inside a sharable zip
    # would leak the user's secret.
    zip_path = output_dir / "quickhatch-migration-bundle.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in output_dir.iterdir():
            if file.name != zip_path.name and not file.name.startswith(".") and file.is_file():
                zf.write(file, file.name)

    print_success(f"\nMigration bundle created: {zip_path}")

    console.print(
        f"\n[bold green]Migration bundle ready![/bold green]\n\n"
        f"Location: [cyan]{output_dir}[/cyan]\n\n"
        f"Files:\n"
        f"  - MIGRATION_INSTRUCTIONS.md  (AI agent instructions)\n"
        f"  - START_HERE.md             (practical boot/install checklist)\n"
        f"  - quickhatch-profile.json    (machine-readable profile)\n"
        f"  - quickhatch-profile.yaml    (human-readable profile)\n"
        f"  - quickhatch-handoff.json    (portable profile + plan checkpoint)\n"
        f"  - quickstart.sh              (quick-start script for Linux)\n"
        f"  - quickhatch-migration-bundle.zip (all-in-one archive)\n\n"
        f"[bold]Next steps:[/bold]\n"
        f"  1. Read [cyan]START_HERE.md[/cyan] in the bundle\n"
        f"  2. Boot the target machine from the USB installer\n"
        f"  3. Extract/copy the bundle onto the target machine\n"
        f"  4. Follow the mode-specific steps from START_HERE.md\n"
    )

    profile.completed_steps.append("export")


def _generate_quickstart(profile: UserProfile) -> str:
    """Generate a shell script that prepares the Linux target for agent SSH.

    Works in two scenarios:

    1. **Live USB** — user booted from a distro ISO without installing.
       Script installs sshd if missing, starts it (via systemctl / OpenRC /
       runit / s6 / dinit depending on the init in the live env), adds the
       public key to root's and the live user's authorized_keys. Agent
       detects the live env and drives the install itself.

    2. **Already-installed system** — user went through the distro's
       normal installer and created an account. Script does the same
       setup but against that user's home directory.

    In both cases, prints a machine-readable environment marker and the
    IP + username the user should paste back into QuickHatch.
    """
    import shlex as _shlex

    _raw_pub_name = Path(profile.ssh_public_key_path).name if profile.ssh_public_key_path else ""
    # Quote ONCE and assign to a shell var. Every downstream reference
    # uses "$KEY_FILE" so there's no interaction between the Python
    # f-string interpolation and the surrounding quoting in the shell
    # script. Handles pub_name with apostrophes, whitespace, or other
    # shell metacharacters safely.
    pub_name_quoted = _shlex.quote(_raw_pub_name) if _raw_pub_name else ""
    key_block = ""
    if _raw_pub_name:
        key_block = f"""
# --- Install SSH key into the current user's authorized_keys ---
KEY_FILE="$SCRIPT_DIR"/{pub_name_quoted}
USER_HOME="$HOME"
if [ "$(id -u)" = "0" ] && [ -n "${{SUDO_USER:-}}" ] && [ "${{SUDO_USER}}" != "root" ]; then
    # Running under sudo — install into the invoking user, not root
    USER_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)
fi
mkdir -p "$USER_HOME/.ssh"
chmod 700 "$USER_HOME/.ssh"
if [ -f "$KEY_FILE" ]; then
    grep -qxF "$(cat "$KEY_FILE")" "$USER_HOME/.ssh/authorized_keys" 2>/dev/null \\
        || cat "$KEY_FILE" >> "$USER_HOME/.ssh/authorized_keys"
    chmod 600 "$USER_HOME/.ssh/authorized_keys"
    if [ "$(id -u)" = "0" ] && [ -n "${{SUDO_USER:-}}" ]; then
        chown -R "$SUDO_USER" "$USER_HOME/.ssh"
    fi
    echo "[+] SSH key added to $USER_HOME/.ssh/authorized_keys"
else
    echo "[!] Warning: $KEY_FILE not found in this directory."
    echo "    Make sure you ran this script from the extracted bundle."
fi

# Also install into root's authorized_keys on a live env (so the agent
# can install the distro from scratch without needing a user account).
if [ -f "$KEY_FILE" ]; then
    if [ "$(id -u)" = "0" ]; then
        mkdir -p /root/.ssh
        chmod 700 /root/.ssh
        grep -qxF "$(cat "$KEY_FILE")" /root/.ssh/authorized_keys 2>/dev/null \\
            || cat "$KEY_FILE" >> /root/.ssh/authorized_keys
        chmod 600 /root/.ssh/authorized_keys
    elif command -v sudo >/dev/null 2>&1; then
        sudo mkdir -p /root/.ssh
        sudo chmod 700 /root/.ssh
        sudo bash -c 'grep -qxF "$(cat "$1")" /root/.ssh/authorized_keys 2>/dev/null || cat "$1" >> /root/.ssh/authorized_keys' _ "$KEY_FILE"
        sudo chmod 600 /root/.ssh/authorized_keys
    fi
fi
"""

    return (
        r"""#!/usr/bin/env bash
# QuickHatch — Linux Preparation Script
# Run this on the Linux target (live USB OR installed system).
# The AI agent on your Windows PC will SSH in and do the rest.

set -e

SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd -P) || SCRIPT_DIR="$PWD"

echo "========================================"
echo "  QuickHatch — Preparing Linux"
echo "========================================"
echo ""

# --- Detect live ISO vs installed system ---
IS_LIVE=no
if grep -qE '\b(boot=live|live-media|toram|rd\.live\.image|archisolabel|archisodevice|boot=casper)\b' /proc/cmdline 2>/dev/null; then
    IS_LIVE=yes
fi
for marker in /run/live /rofs /cdrom/casper /run/archiso /lib/live /run/calamares; do
    [ -d "$marker" ] && IS_LIVE=yes
done
if mount 2>/dev/null | grep -qE ' on / type (overlay|aufs|squashfs|tmpfs)\b'; then
    IS_LIVE=yes
fi

if [ "$IS_LIVE" = "yes" ]; then
    echo "[*] Detected LIVE USB environment."
    echo "    The agent will install the distro from scratch via SSH."
else
    echo "[*] Detected INSTALLED system."
    echo "    The agent will configure the existing install via SSH."
fi
echo ""

# --- Live ISO privilege continuity ---
# Live ISO users are often passwordless locally, but not necessarily
# passwordless over SSH. Grant a temporary live-session sudoers drop-in so
# later remote setup commands do not hang or fail on invisible sudo prompts.
if [ "$IS_LIVE" = "yes" ] && [ "$(id -u)" != "0" ] && command -v sudo >/dev/null 2>&1; then
    LIVE_USER=$(id -un)
    echo "[*] Granting temporary NOPASSWD sudo for live user: $LIVE_USER"
    sudo sh -c 'printf "%s\n" "$1 ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/99-quickhatch-live-nopasswd && chmod 440 /etc/sudoers.d/99-quickhatch-live-nopasswd' _ "$LIVE_USER"
fi

# --- Ensure SSH server is installed ---
if ! command -v sshd >/dev/null 2>&1; then
    echo "[*] Installing SSH server..."
    if command -v apt >/dev/null 2>&1; then
        sudo apt install -y openssh-server
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y openssh-server
    elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -S --noconfirm openssh
    elif command -v zypper >/dev/null 2>&1; then
        sudo zypper install -y openssh
    elif command -v apk >/dev/null 2>&1; then
        sudo apk add openssh
    elif command -v xbps-install >/dev/null 2>&1; then
        sudo xbps-install -Sy openssh
    else
        echo "[!] Could not identify the package manager. Install openssh-server manually."
        exit 1
    fi
fi
"""
        + key_block
        + r"""
# --- Ensure SSH accepts the QuickHatch key on live/root sessions ---
if command -v sshd >/dev/null 2>&1 && [ "$(id -u)" = "0" ]; then
    mkdir -p /etc/ssh/sshd_config.d
    {
        echo "PubkeyAuthentication yes"
        echo "AuthorizedKeysFile .ssh/authorized_keys"
        if [ "$IS_LIVE" = "yes" ]; then
            echo "PermitRootLogin prohibit-password"
        fi
    } > /etc/ssh/sshd_config.d/99-quickhatch-pubkey.conf
fi

# --- Enable + start the SSH daemon across init systems ---
START_OK=no
if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl enable --now sshd 2>/dev/null \
        || sudo systemctl enable --now ssh 2>/dev/null \
        && START_OK=yes
fi
if [ "$START_OK" = "no" ] && command -v rc-service >/dev/null 2>&1; then
    # OpenRC (Artix OpenRC, Alpine, Gentoo-OpenRC)
    sudo rc-update add sshd default 2>/dev/null \
        || sudo rc-update add ssh default 2>/dev/null || true
    sudo rc-service sshd start 2>/dev/null \
        || sudo rc-service ssh start 2>/dev/null || true
    START_OK=yes
fi
if [ "$START_OK" = "no" ] && command -v sv >/dev/null 2>&1; then
    # runit (Void, Artix-runit)
    sudo ln -sf /etc/sv/sshd /var/service/ 2>/dev/null || \
        sudo ln -sf /etc/runit/sv/sshd /var/service/ 2>/dev/null || true
    START_OK=yes
fi
if [ "$START_OK" = "no" ] && command -v dinitctl >/dev/null 2>&1; then
    # dinit (Chimera, Artix-dinit)
    sudo dinitctl enable sshd 2>/dev/null || true
    START_OK=yes
fi
if [ "$START_OK" = "no" ]; then
    # Last resort — just launch the daemon directly for this session
    sudo /usr/sbin/sshd 2>/dev/null || sudo sshd 2>/dev/null || true
fi
echo "[+] SSH daemon enabled and running"
echo ""

# --- Report environment marker + IP for the user ---
echo "========================================"
echo "  Ready for remote setup!"
echo "========================================"
echo ""
USERNAME=$(whoami)

# Pull the first non-loopback IPv4 address. Multiple tools tried in
# order — none of them are guaranteed to exist or behave identically
# across distros. Prior versions only used `hostname -I` and
# `ip route get 1`, both of which:
#   - exit 0 on success even when their output is empty (so the
#     `||` chain never falls through to the next strategy)
#   - depend on packages that aren't always present on minimal live
#     ISOs (`hostname` is part of inetutils; missing on stock Arch)
# This version reads the address directly from `ip -4 addr` which is
# always available on any Linux that boots, then falls back to the
# original tools, then to "unknown".
_ip_pick() {
    # ip(8) — most portable. Prints first non-loopback IPv4 sans CIDR.
    if command -v ip >/dev/null 2>&1; then
        local ip
        ip=$(ip -4 -o addr show scope global 2>/dev/null \
             | awk '{print $4}' | sed 's|/.*||' | head -n1)
        [ -n "$ip" ] && { echo "$ip"; return 0; }
        # Fallback: route to a public address.
        ip=$(ip route get 1.1.1.1 2>/dev/null \
             | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')
        [ -n "$ip" ] && { echo "$ip"; return 0; }
    fi
    if command -v hostname >/dev/null 2>&1; then
        local ip
        ip=$(hostname -I 2>/dev/null | awk '{print $1}')
        [ -n "$ip" ] && { echo "$ip"; return 0; }
    fi
    echo "unknown"
}
IP=$(_ip_pick)

echo "[QuickHatch] Environment: $(echo $IS_LIVE | tr a-z A-Z)"
echo "[QuickHatch] Username:    $USERNAME"
echo "[QuickHatch] IP address:  $IP"
echo ""
echo "Paste the IP + username into QuickHatch's Remote Setup step."
if [ "$IS_LIVE" = "yes" ]; then
    echo ""
    echo "NOTE: You're in a live USB. Leave this terminal open — the agent"
    echo "      will install the distro, reboot, and reconnect. Don't touch"
    echo "      the keyboard unless the agent asks you to."
fi
"""
    )
