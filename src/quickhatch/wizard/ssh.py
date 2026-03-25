"""Step 7: SSH key generation for AI agent handoff."""

import os
import subprocess
from pathlib import Path

import questionary
from rich.panel import Panel

from quickhatch.config import UserProfile
from quickhatch.console import console, print_step, print_success, print_warning, print_info


def run(profile: UserProfile) -> None:
    """Run the SSH key setup step."""
    print_step(7, "SSH Key Setup")

    console.print(
        "SSH keys allow the AI agent to securely connect to your\n"
        "new Linux system and continue the setup process remotely.\n"
        "This is the easiest path to letting the agent work for you.\n"
    )

    # Check for existing SSH keys
    ssh_dir = Path.home() / ".ssh"
    existing_keys = []
    if ssh_dir.exists():
        for key_file in ssh_dir.glob("*.pub"):
            existing_keys.append(key_file)

    if existing_keys:
        console.print("[info]Existing SSH keys found:[/info]")
        for key in existing_keys:
            console.print(f"  - {key}")
        console.print()

        use_existing = questionary.confirm(
            "Use an existing SSH key?",
            default=True,
        ).ask()

        if use_existing:
            if len(existing_keys) == 1:
                chosen = existing_keys[0]
            else:
                chosen_path = questionary.select(
                    "Which key to use?",
                    choices=[str(k) for k in existing_keys],
                ).ask()
                chosen = Path(chosen_path)

            profile.ssh_public_key_path = str(chosen)
            print_success(f"Using existing key: {chosen}")
            _show_instructions(profile)
            profile.completed_steps.append("ssh")
            return

    # Generate new key
    generate = questionary.confirm(
        "Generate a new SSH key pair for the migration?",
        default=True,
    ).ask()

    if not generate:
        print_info("Skipping SSH setup. You can set it up manually later.")
        profile.completed_steps.append("ssh")
        return

    # Key options
    key_type = questionary.select(
        "Key type:",
        choices=[
            questionary.Choice("Ed25519 (recommended, modern)", value="ed25519"),
            questionary.Choice("RSA 4096-bit (maximum compatibility)", value="rsa"),
        ],
    ).ask()

    key_name = questionary.text(
        "Key filename:",
        default="quickhatch_migration",
    ).ask()

    ssh_dir.mkdir(parents=True, exist_ok=True)
    key_path = ssh_dir / key_name

    if key_path.exists():
        print_warning(f"Key {key_path} already exists!")
        overwrite = questionary.confirm("Overwrite?", default=False).ask()
        if not overwrite:
            profile.ssh_public_key_path = str(key_path.with_suffix(".pub"))
            profile.completed_steps.append("ssh")
            return

    # Generate key
    try:
        cmd = ["ssh-keygen", "-t", key_type]
        if key_type == "rsa":
            cmd.extend(["-b", "4096"])
        cmd.extend([
            "-f", str(key_path),
            "-N", "",  # empty passphrase for automated use
            "-C", "quickhatch-migration",
        ])
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode == 0:
            pub_key_path = key_path.with_suffix(".pub")
            profile.ssh_public_key_path = str(pub_key_path)
            print_success(f"SSH key pair generated!")
            console.print(f"  Private key: {key_path}")
            console.print(f"  Public key:  {pub_key_path}")
        else:
            print_warning(f"ssh-keygen failed: {result.stderr}")
            print_info("You can generate SSH keys manually later with: ssh-keygen -t ed25519")
    except FileNotFoundError:
        print_warning("ssh-keygen not found. Install OpenSSH to generate keys.")
        print_info("On Windows: Settings > Apps > Optional Features > OpenSSH Client")

    _show_instructions(profile)
    profile.completed_steps.append("ssh")


def _show_instructions(profile: UserProfile):
    """Show instructions for using the SSH key on the new system."""
    console.print()
    console.print(Panel(
        "[bold]After installing Linux, the AI agent will:[/bold]\n\n"
        "1. Copy the SSH public key to your new system\n"
        "2. Enable SSH server: [cyan]sudo apt install openssh-server[/cyan]\n"
        "3. Add key to authorized_keys:\n"
        "   [cyan]mkdir -p ~/.ssh && cat quickhatch_migration.pub >> ~/.ssh/authorized_keys[/cyan]\n"
        "4. Connect and continue the setup automatically\n\n"
        "[dim]The public key will be included in your migration bundle.[/dim]",
        title="SSH Handoff Plan",
        border_style="cyan",
    ))
