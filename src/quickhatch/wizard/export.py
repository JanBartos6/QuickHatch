"""Step 8: Export migration bundle and generate LLM instructions."""

import json
import zipfile
from datetime import datetime
from pathlib import Path

import questionary
import yaml

from quickhatch.config import UserProfile
from quickhatch.console import console, print_step, print_success, print_info
from quickhatch.ai.instruction_gen import generate_instructions


def run(profile: UserProfile) -> None:
    """Run the export step."""
    print_step(8, "Export Migration Bundle")

    console.print(
        "We'll now export everything into a migration bundle.\n"
        "This includes your profile, LLM instructions, and SSH keys.\n"
    )

    # Choose export location
    default_path = str(Path.home() / "Desktop" / "quickhatch-migration")
    export_dir = questionary.text(
        "Export location:",
        default=default_path,
    ).ask()

    if export_dir is None:
        raise KeyboardInterrupt

    export_path = Path(export_dir)
    export_path.mkdir(parents=True, exist_ok=True)

    # 1. Save profile as JSON
    profile_path = export_path / "profile.json"
    profile_path.write_text(json.dumps(profile.to_dict(), indent=2, default=str))
    print_success(f"Profile saved: {profile_path}")

    # 2. Save profile as YAML (human-readable)
    yaml_path = export_path / "profile.yaml"
    yaml_path.write_text(yaml.dump(profile.to_dict(), default_flow_style=False, sort_keys=False))
    print_success(f"Human-readable profile: {yaml_path}")

    # 3. Generate LLM instruction files
    instructions = generate_instructions(profile)

    instructions_path = export_path / "INSTRUCTIONS.md"
    instructions_path.write_text(instructions)
    print_success(f"LLM instructions: {instructions_path}")

    # 4. Generate a quick-start script
    quickstart = _generate_quickstart_script(profile)
    script_path = export_path / "quickstart.sh"
    script_path.write_text(quickstart)
    print_success(f"Quick-start script: {script_path}")

    # 5. Copy SSH public key if available
    if profile.ssh_public_key_path:
        ssh_pub = Path(profile.ssh_public_key_path)
        if ssh_pub.exists():
            dest = export_path / ssh_pub.name
            dest.write_text(ssh_pub.read_text())
            print_success(f"SSH public key: {dest}")

    # 6. Create a zip bundle
    zip_path = export_path.with_suffix(".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in export_path.rglob("*"):
            if file.is_file():
                zf.write(file, file.relative_to(export_path))
    print_success(f"Migration bundle: {zip_path}")

    console.print()
    console.print(f"[bold green]Migration bundle ready![/bold green]")
    console.print()
    console.print(
        f"Copy [highlight]{zip_path}[/highlight] to your new Linux system.\n"
        f"Then run:\n"
        f"  [cyan]unzip {zip_path.name}[/cyan]\n"
        f"  [cyan]chmod +x quickstart.sh && ./quickstart.sh[/cyan]\n"
    )

    profile.completed_steps.append("export")


def _generate_quickstart_script(profile: UserProfile) -> str:
    """Generate a bash script to bootstrap the AI agent on Linux."""
    ai = profile.ai_config
    distro = profile.recommended_distro or "linux"

    script = f"""#!/usr/bin/env bash
# QuickHatch Migration Quick-Start Script
# Generated: {datetime.now().isoformat()}
# Target distro: {distro}

set -euo pipefail

echo "==================================="
echo "  QuickHatch Migration Quick-Start"
echo "==================================="
echo ""

# Ensure we're running on Linux
if [[ "$(uname -s)" != "Linux" ]]; then
    echo "ERROR: This script must be run on Linux!"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"

# Update system
echo "[1/5] Updating system packages..."
if command -v apt &>/dev/null; then
    sudo apt update && sudo apt upgrade -y
elif command -v dnf &>/dev/null; then
    sudo dnf upgrade -y
elif command -v pacman &>/dev/null; then
    sudo pacman -Syu --noconfirm
fi

# Install essential tools
echo "[2/5] Installing essential tools..."
if command -v apt &>/dev/null; then
    sudo apt install -y curl wget git openssh-server
elif command -v dnf &>/dev/null; then
    sudo dnf install -y curl wget git openssh-server
elif command -v pacman &>/dev/null; then
    sudo pacman -S --noconfirm curl wget git openssh
fi

# Enable SSH
echo "[3/5] Setting up SSH..."
sudo systemctl enable --now sshd 2>/dev/null || sudo systemctl enable --now ssh 2>/dev/null || true
"""

    # SSH key setup
    if profile.ssh_public_key_path:
        key_name = Path(profile.ssh_public_key_path).name
        script += f"""
# Add migration SSH key
if [[ -f "$SCRIPT_DIR/{key_name}" ]]; then
    mkdir -p ~/.ssh
    chmod 700 ~/.ssh
    cat "$SCRIPT_DIR/{key_name}" >> ~/.ssh/authorized_keys
    chmod 600 ~/.ssh/authorized_keys
    echo "  SSH key installed from migration bundle."
fi
"""

    # AI agent setup
    script += '\necho "[4/5] Setting up AI assistant..."\n'

    if ai.provider == "claude_code":
        script += """
# Install Node.js if needed
if ! command -v node &>/dev/null; then
    curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
    sudo apt install -y nodejs 2>/dev/null || sudo dnf install -y nodejs 2>/dev/null
fi
# Install Claude Code
npm install -g @anthropic-ai/claude-code
echo "  Claude Code installed. Run 'claude' to start."
"""
        if ai.api_key:
            script += f'export ANTHROPIC_API_KEY="{ai.api_key}"\n'

    elif ai.provider == "codex":
        script += """
# Install Codex
npm install -g @openai/codex
echo "  Codex installed. Run 'codex' to start."
"""
        if ai.api_key:
            script += f'export OPENAI_API_KEY="{ai.api_key}"\n'

    elif ai.provider == "local_ollama":
        model = ai.model_name or "llama3"
        script += f"""
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh
ollama pull {model}
echo "  Ollama installed with model: {model}"
"""

    elif ai.provider == "local_lmstudio":
        script += """
echo "  Please install LM Studio manually from: https://lmstudio.ai"
echo "  Then start the local server and load your preferred model."
"""

    elif ai.provider == "api":
        if ai.api_key:
            script += f'export OPENAI_API_KEY="{ai.api_key}"\n'
        if ai.endpoint:
            script += f'export OPENAI_BASE_URL="{ai.endpoint}"\n'

    # Launch agent with instructions
    script += f"""
# Provide instructions to the AI
echo "[5/5] Migration bundle ready!"
echo ""
echo "Your migration profile and instructions are in: $SCRIPT_DIR"
echo ""
echo "To start the AI-assisted setup, run:"
"""

    if ai.provider == "claude_code":
        script += """echo "  cd $SCRIPT_DIR && claude"
echo ""
echo "Then tell Claude: 'Read INSTRUCTIONS.md and set up my system'"
"""
    elif ai.provider == "codex":
        script += """echo "  cd $SCRIPT_DIR && codex"
echo ""
echo "Then tell Codex: 'Read INSTRUCTIONS.md and set up my system'"
"""
    elif ai.provider == "local_ollama":
        script += """echo "  cd $SCRIPT_DIR && ollama run {model}"
echo ""
echo "Then paste the contents of INSTRUCTIONS.md"
""".replace("{model}", ai.model_name or "llama3")
    else:
        script += """echo "  Start your AI assistant and provide it with INSTRUCTIONS.md"
"""

    script += """
echo ""
echo "==================================="
echo "  QuickHatch setup complete!"
echo "==================================="
"""
    return script
