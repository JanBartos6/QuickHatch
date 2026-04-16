"""Step 8: Export migration bundle."""

from __future__ import annotations

import json
import zipfile
from dataclasses import asdict
from pathlib import Path

import questionary
import yaml

from quickhatch.ai.instruction_gen import save_instructions
from quickhatch.config import UserProfile
from quickhatch.console import console, print_info, print_step, print_success


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

    # Generate instruction file
    print_info("Generating AI instruction file...")
    inst_path = save_instructions(profile, output_dir)
    print_success(f"Instructions saved: {inst_path}")

    # Save profile as JSON
    print_info("Saving profile data...")
    profile_path = output_dir / "quickhatch-profile.json"
    # Exclude sensitive data like API keys from the JSON dump
    profile_dict = asdict(profile)
    if profile_dict.get("ai_config", {}).get("api_key"):
        profile_dict["ai_config"]["api_key"] = "***STORED_SEPARATELY***"
    profile_path.write_text(json.dumps(profile_dict, indent=2))

    # Save as YAML for human readability
    yaml_path = output_dir / "quickhatch-profile.yaml"
    yaml_path.write_text(yaml.dump(profile_dict, default_flow_style=False, sort_keys=False))

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
    quickstart_path.write_text(_generate_quickstart(profile))
    print_info(f"Quick-start script saved: {quickstart_path}")

    # Create ZIP bundle
    zip_path = output_dir / "quickhatch-migration-bundle.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in output_dir.iterdir():
            if file.name != zip_path.name and file.is_file():
                zf.write(file, file.name)

    print_success(f"\nMigration bundle created: {zip_path}")

    console.print(
        f"\n[bold green]Migration bundle ready![/bold green]\n\n"
        f"Location: [cyan]{output_dir}[/cyan]\n\n"
        f"Files:\n"
        f"  - MIGRATION_INSTRUCTIONS.md  (AI agent instructions)\n"
        f"  - quickhatch-profile.json    (machine-readable profile)\n"
        f"  - quickhatch-profile.yaml    (human-readable profile)\n"
        f"  - quickstart.sh              (quick-start script for Linux)\n"
        f"  - quickhatch-migration-bundle.zip (all-in-one archive)\n\n"
        f"[bold]Next steps:[/bold]\n"
        f"  1. Install Linux using your bootable USB\n"
        f"  2. Copy the bundle to your new Linux machine (USB or network)\n"
        f"  3. On Linux, run: [cyan]bash quickstart.sh[/cyan]\n"
        f"     This sets up SSH so the agent can connect remotely.\n"
        f"  4. Note the IP address and username it prints\n"
        f"  5. Back on Windows, the AI agent will SSH into Linux and set it up!\n"
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
    pub_name = Path(profile.ssh_public_key_path).name if profile.ssh_public_key_path else ""
    key_block = ""
    if pub_name:
        key_block = f"""
# --- Install SSH key into the current user's authorized_keys ---
USER_HOME="$HOME"
if [ "$(id -u)" = "0" ] && [ -n "${{SUDO_USER:-}}" ] && [ "${{SUDO_USER}}" != "root" ]; then
    # Running under sudo — install into the invoking user, not root
    USER_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)
fi
mkdir -p "$USER_HOME/.ssh"
chmod 700 "$USER_HOME/.ssh"
if [ -f "{pub_name}" ]; then
    grep -qxF "$(cat {pub_name})" "$USER_HOME/.ssh/authorized_keys" 2>/dev/null \\
        || cat "{pub_name}" >> "$USER_HOME/.ssh/authorized_keys"
    chmod 600 "$USER_HOME/.ssh/authorized_keys"
    if [ "$(id -u)" = "0" ] && [ -n "${{SUDO_USER:-}}" ]; then
        chown -R "$SUDO_USER" "$USER_HOME/.ssh"
    fi
    echo "[+] SSH key added to $USER_HOME/.ssh/authorized_keys"
else
    echo "[!] Warning: {pub_name} not found in this directory."
    echo "    Make sure you ran this script from the extracted bundle."
fi

# Also install into root's authorized_keys on a live env (so the agent
# can install the distro from scratch without needing a user account).
if [ -f "{pub_name}" ]; then
    if [ "$(id -u)" = "0" ]; then
        mkdir -p /root/.ssh
        chmod 700 /root/.ssh
        grep -qxF "$(cat {pub_name})" /root/.ssh/authorized_keys 2>/dev/null \\
            || cat "{pub_name}" >> /root/.ssh/authorized_keys
        chmod 600 /root/.ssh/authorized_keys
    elif command -v sudo >/dev/null 2>&1; then
        sudo mkdir -p /root/.ssh
        sudo chmod 700 /root/.ssh
        sudo bash -c "grep -qxF \\"$(cat {pub_name})\\" /root/.ssh/authorized_keys 2>/dev/null || cat '{pub_name}' >> /root/.ssh/authorized_keys"
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
IP=$(hostname -I 2>/dev/null | awk '{print $1}' || \
     ip route get 1 2>/dev/null | awk '{print $7; exit}' || \
     echo "unknown")

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
