"""Step 7: SSH key generation for AI agent handoff."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import questionary

from quickhatch.config import UserProfile
from quickhatch.console import console, print_error, print_info, print_step, print_success, print_warning


def _find_ssh_keygen() -> str | None:
    """Find ssh-keygen executable."""
    import shutil

    path = shutil.which("ssh-keygen")
    if path:
        return path

    # Windows common locations
    if sys.platform == "win32":
        candidates = [
            Path(r"C:\Windows\System32\OpenSSH\ssh-keygen.exe"),
            Path(r"C:\Program Files\Git\usr\bin\ssh-keygen.exe"),
            Path(r"C:\Program Files (x86)\Git\usr\bin\ssh-keygen.exe"),
        ]
        for c in candidates:
            if c.exists():
                return str(c)
    return None


def run(profile: UserProfile) -> None:
    print_step(7, "SSH Key Setup")

    console.print(
        "SSH keys allow the AI agent to securely connect to your new Linux\n"
        "machine and continue the setup process remotely.\n"
    )

    # Check for existing keys
    ssh_dir = Path.home() / ".ssh"
    existing_keys = []
    if ssh_dir.exists():
        for f in ssh_dir.iterdir():
            if f.suffix == ".pub":
                existing_keys.append(f)

    if existing_keys:
        console.print("[bold]Existing SSH keys found:[/bold]")
        for k in existing_keys:
            console.print(f"  - {k.name}")
        console.print()

        use_existing = questionary.confirm(
            "Would you like to use an existing key?", default=True
        ).ask()
        if use_existing is None:
            raise KeyboardInterrupt

        if use_existing:
            if len(existing_keys) == 1:
                chosen = existing_keys[0]
            else:
                chosen_name = questionary.select(
                    "Which key?",
                    choices=[k.name for k in existing_keys],
                ).ask()
                if chosen_name is None:
                    raise KeyboardInterrupt
                chosen = ssh_dir / chosen_name

            profile.ssh_public_key_path = str(chosen)
            print_success(f"Using existing key: {chosen}")
            profile.completed_steps.append("ssh")
            return

    # Generate new key
    generate = questionary.confirm(
        "Generate a new SSH key pair for the migration?", default=True
    ).ask()
    if generate is None:
        raise KeyboardInterrupt

    if not generate:
        print_info("Skipping SSH key generation. You can set it up manually later.")
        profile.completed_steps.append("ssh")
        return

    ssh_keygen = _find_ssh_keygen()
    if not ssh_keygen:
        print_error(
            "ssh-keygen not found. Install OpenSSH or Git for Windows, "
            "then re-run this step."
        )
        console.print(
            "\n[bold]To install OpenSSH on Windows:[/bold]\n"
            "  Settings > Apps > Optional Features > Add a Feature > OpenSSH Client\n"
            "\n[bold]Or install Git for Windows:[/bold]\n"
            "  https://git-scm.com/download/win\n"
        )
        profile.completed_steps.append("ssh")
        return

    key_path = ssh_dir / "quickhatch_ed25519"
    ssh_dir.mkdir(parents=True, exist_ok=True)

    comment = questionary.text(
        "Key comment (e.g., your email):",
        default="quickhatch-migration",
    ).ask()
    if comment is None:
        raise KeyboardInterrupt

    try:
        result = subprocess.run(
            [
                ssh_keygen,
                "-t", "ed25519",
                "-C", comment,
                "-f", str(key_path),
                "-N", "",  # No passphrase for agent use
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode == 0:
            pub_key_path = Path(f"{key_path}.pub")
            profile.ssh_public_key_path = str(pub_key_path)

            pub_key = pub_key_path.read_text().strip()
            print_success("SSH key pair generated!")
            console.print(f"\n[bold]Private key:[/bold] {key_path}")
            console.print(f"[bold]Public key:[/bold]  {pub_key_path}")
            console.print(f"\n[dim]{pub_key}[/dim]\n")

            console.print(
                "[bold]Next steps on your new Linux machine:[/bold]\n"
                "  1. Copy the public key to the Linux machine\n"
                "  2. Add it to ~/.ssh/authorized_keys\n"
                "  3. Enable SSH: sudo systemctl enable --now sshd\n"
                "  4. The AI agent can then connect and continue setup\n"
            )
        else:
            print_error(f"ssh-keygen failed: {result.stderr}")

    except subprocess.TimeoutExpired:
        print_error("ssh-keygen timed out")
    except OSError as e:
        print_error(f"Could not run ssh-keygen: {e}")

    profile.completed_steps.append("ssh")
