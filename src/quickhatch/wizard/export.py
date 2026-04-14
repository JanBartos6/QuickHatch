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
    """Generate a shell script to prepare the Linux machine for remote setup.

    This script runs on the NEW Linux machine. It only sets up SSH access
    so the AI agent (running on Windows) can connect remotely to do the
    actual configuration.
    """
    script = "#!/usr/bin/env bash\n"
    script += "# QuickHatch — Linux Preparation Script\n"
    script += "# Run this on your new Linux machine to prepare it for remote setup.\n"
    script += "# The AI agent on your Windows PC will SSH in and do the rest.\n\n"
    script += "set -e\n\n"
    script += 'echo "========================================"\n'
    script += 'echo "  QuickHatch — Preparing Linux"\n'
    script += 'echo "========================================"\n'
    script += 'echo ""\n\n'

    # Install openssh-server if needed
    script += "# Ensure SSH server is installed\n"
    script += "if ! command -v sshd &> /dev/null; then\n"
    script += '    echo "[*] Installing SSH server..."\n'
    script += "    sudo apt install -y openssh-server 2>/dev/null || \\\n"
    script += "    sudo dnf install -y openssh-server 2>/dev/null || \\\n"
    script += "    sudo pacman -S --noconfirm openssh 2>/dev/null || \\\n"
    script += "    sudo zypper install -y openssh 2>/dev/null || true\n"
    script += "fi\n\n"

    # SSH key setup
    if profile.ssh_public_key_path:
        pub_name = Path(profile.ssh_public_key_path).name
        script += "# Add the QuickHatch SSH key\n"
        script += 'echo "[*] Setting up SSH access..."\n'
        script += "mkdir -p ~/.ssh && chmod 700 ~/.ssh\n"
        script += f'if [ -f "{pub_name}" ]; then\n'
        script += f'    grep -qxF "$(cat {pub_name})" ~/.ssh/authorized_keys 2>/dev/null || cat "{pub_name}" >> ~/.ssh/authorized_keys\n'
        script += "    chmod 600 ~/.ssh/authorized_keys\n"
        script += '    echo "[+] SSH key added to authorized_keys"\n'
        script += "else\n"
        script += f'    echo "[!] Warning: {pub_name} not found in this directory."\n'
        script += '    echo "    Make sure you copied the full migration bundle."\n'
        script += "fi\n\n"

    # Enable SSH daemon
    script += "# Enable and start SSH daemon\n"
    script += "if command -v systemctl &> /dev/null; then\n"
    script += "    sudo systemctl enable --now sshd 2>/dev/null || sudo systemctl enable --now ssh 2>/dev/null || true\n"
    script += '    echo "[+] SSH daemon enabled and running"\n'
    script += "fi\n\n"

    # Print connection info for the user to give back to the Windows agent
    script += "# Show connection info\n"
    script += 'echo ""\n'
    script += 'echo "========================================"\n'
    script += 'echo "  Ready for remote setup!"\n'
    script += 'echo "========================================"\n'
    script += 'echo ""\n'
    script += 'echo "Give the AI agent on Windows these details:"\n'
    script += 'echo ""\n'
    script += "USERNAME=$(whoami)\n"
    script += 'echo "  Username: $USERNAME"\n'
    script += "# Try to get the local IP address\n"
    script += 'IP=$(hostname -I 2>/dev/null | awk "{print \\$1}" || ip route get 1 2>/dev/null | awk "{print \\$7;exit}" || echo "unknown")\n'
    script += 'echo "  IP address: $IP"\n'
    script += 'echo ""\n'
    script += 'echo "The agent will connect with:"\n'
    script += 'echo "  ssh $USERNAME@$IP"\n'
    script += 'echo ""\n'
    script += 'echo "You can now go back to QuickHatch on Windows."\n'

    return script
