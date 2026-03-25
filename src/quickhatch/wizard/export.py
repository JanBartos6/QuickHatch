"""Step 8: Export migration bundle."""

from __future__ import annotations

import json
import zipfile
from dataclasses import asdict
from pathlib import Path

import questionary
import yaml

from quickhatch.ai.instruction_gen import generate_instructions, save_instructions
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

    # Store API key separately if present
    if profile.ai_config.api_key:
        api_key_path = output_dir / ".api_key"
        api_key_path.write_text(profile.ai_config.api_key)
        print_info("API key saved to .api_key (keep this secure!)")

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
        f"  1. Copy the bundle to a USB drive or cloud storage\n"
        f"  2. Install Linux using your bootable USB\n"
        f"  3. Copy the bundle to your new Linux system\n"
        f"  4. Run: [cyan]bash quickstart.sh[/cyan]\n"
        f"  5. The AI agent will take it from there!\n"
    )

    profile.completed_steps.append("export")


def _generate_quickstart(profile: UserProfile) -> str:
    """Generate a shell script to bootstrap the AI agent on Linux."""
    ai = profile.ai_config

    script = "#!/usr/bin/env bash\n"
    script += "# QuickHatch Migration Quick-Start Script\n"
    script += "# Run this on your new Linux system to start the AI-guided setup.\n\n"
    script += 'set -e\n\n'
    script += 'echo "========================================"\n'
    script += 'echo "  QuickHatch Migration Setup"\n'
    script += 'echo "========================================"\n'
    script += 'echo ""\n\n'

    # SSH setup
    if profile.ssh_public_key_path:
        pub_name = Path(profile.ssh_public_key_path).name
        script += "# Set up SSH access\n"
        script += 'echo "[*] Setting up SSH access..."\n'
        script += "mkdir -p ~/.ssh && chmod 700 ~/.ssh\n"
        script += f'if [ -f "{pub_name}" ]; then\n'
        script += f'    cat "{pub_name}" >> ~/.ssh/authorized_keys\n'
        script += "    chmod 600 ~/.ssh/authorized_keys\n"
        script += '    echo "[+] SSH key added"\n'
        script += "fi\n\n"
        script += "# Enable SSH daemon\n"
        script += "if command -v systemctl &> /dev/null; then\n"
        script += "    sudo systemctl enable --now sshd 2>/dev/null || sudo systemctl enable --now ssh 2>/dev/null || true\n"
        script += '    echo "[+] SSH daemon enabled"\n'
        script += "fi\n\n"

    # Install AI agent
    if ai.provider == "claude_code":
        script += "# Install Claude Code\n"
        script += 'echo "[*] Installing Claude Code..."\n'
        script += "if ! command -v node &> /dev/null; then\n"
        script += '    echo "[*] Installing Node.js..."\n'
        script += "    curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash - 2>/dev/null\n"
        script += "    sudo apt install -y nodejs 2>/dev/null || sudo dnf install -y nodejs 2>/dev/null || sudo pacman -S --noconfirm nodejs npm 2>/dev/null\n"
        script += "fi\n"
        script += "npm install -g @anthropic-ai/claude-code\n"
        script += 'echo "[+] Claude Code installed"\n'
        script += 'echo ""\n'
        script += 'echo "Run: claude"\n'
        script += 'echo "Then paste the contents of MIGRATION_INSTRUCTIONS.md"\n'

    elif ai.provider == "codex":
        script += "# Install Codex\n"
        script += 'echo "[*] Installing Codex..."\n'
        script += "if ! command -v node &> /dev/null; then\n"
        script += "    curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash - 2>/dev/null\n"
        script += "    sudo apt install -y nodejs 2>/dev/null || sudo dnf install -y nodejs 2>/dev/null\n"
        script += "fi\n"
        script += "npm install -g @openai/codex\n"
        script += 'echo "[+] Codex installed"\n'

    elif ai.provider == "local_ollama":
        script += "# Install Ollama\n"
        script += 'echo "[*] Installing Ollama..."\n'
        script += "curl -fsSL https://ollama.ai/install.sh | sh\n"
        model = ai.model_name or "llama3"
        script += f'echo "[*] Pulling model {model}..."\n'
        script += f"ollama pull {model}\n"
        script += f'echo "[+] Ollama ready with {model}"\n'

    elif ai.provider == "local_lmstudio":
        script += "# LM Studio\n"
        script += 'echo "[*] Please install LM Studio from: https://lmstudio.ai"\n'
        script += 'echo "    Then load your preferred model and start the local server."\n'

    elif ai.provider == "api":
        script += "# Set up API key\n"
        script += 'if [ -f ".api_key" ]; then\n'
        if ai.endpoint and "anthropic" in ai.endpoint:
            script += '    export ANTHROPIC_API_KEY=$(cat .api_key)\n'
            script += '    echo "[+] Anthropic API key loaded"\n'
        else:
            script += '    export OPENAI_API_KEY=$(cat .api_key)\n'
            script += '    echo "[+] OpenAI API key loaded"\n'
        script += "fi\n"

    script += '\necho ""\n'
    script += 'echo "[+] Setup complete! The AI agent is ready to continue your migration."\n'
    script += 'echo "    Feed MIGRATION_INSTRUCTIONS.md to your AI agent to proceed."\n'

    return script
