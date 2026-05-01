from __future__ import annotations

import json
import zipfile

import quickhatch.ai.instruction_gen as instruction_gen
import quickhatch.gui.server as gui_server_mod
from quickhatch.bridge.server import BridgeState
from quickhatch.config import UserProfile
from quickhatch.gui.server import GUIState, _run_export


def test_run_export_cleans_generated_outputs_and_writes_start_here(monkeypatch, tmp_path):
    profile = UserProfile()
    profile.preferences.another_machine = "no"

    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    ssh_key = ssh_dir / "quickhatch_ed25519"
    ssh_key.write_text("private-key-placeholder\n", encoding="utf-8")
    ssh_pub = ssh_dir / "quickhatch_ed25519.pub"
    ssh_pub.write_text("ssh-ed25519 AAAATEST quickhatch\n", encoding="utf-8")
    profile.ssh_public_key_path = str(ssh_pub)

    bridge = BridgeState(profile, persist_path=None)
    bridge.add_suggestion("install_script", "Install", "#!/usr/bin/env bash\necho ready\n")
    bridge.add_suggestion("plan", "Migration Plan", "Install Arch, then KDE.")
    bridge.add_message("agent", "analysis summary", phase="analysis")
    bridge.add_message("agent", "setup command log should not move", phase="setup")
    gui_state = GUIState(profile, bridge)

    output_dir = tmp_path / "export"
    output_dir.mkdir()
    (output_dir / "quickstart.sh").write_text("stale-quickstart", encoding="utf-8")
    (output_dir / "install.sh").write_text("stale-install", encoding="utf-8")
    (output_dir / "old-key.pub").write_text("stale-key", encoding="utf-8")

    def fake_save_instructions(profile_obj, dest_dir):
        path = dest_dir / "MIGRATION_INSTRUCTIONS.md"
        path.write_text("# Plan\n", encoding="utf-8")
        return path

    monkeypatch.setattr(gui_server_mod, "_export_output_dir", lambda: output_dir)
    monkeypatch.setattr(instruction_gen, "save_instructions", fake_save_instructions)

    result = _run_export(gui_state)

    assert result["status"] == "ok"
    assert "START_HERE.md" in result["files"]
    assert "quickhatch-handoff.json" in result["files"]
    assert "install.sh" in result["files"]
    assert not (output_dir / "old-key.pub").exists()

    start_here = (output_dir / "START_HERE.md").read_text(encoding="utf-8")
    assert "install.sh" in start_here
    assert "quickstart.sh" in start_here

    quickstart = (output_dir / "quickstart.sh").read_text(encoding="utf-8")
    assert "stale-quickstart" not in quickstart
    assert "99-quickhatch-live-nopasswd" in quickstart
    assert "NOPASSWD: ALL" in quickstart
    assert 'SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd -P)' in quickstart
    assert 'KEY_FILE="$SCRIPT_DIR"/quickhatch_ed25519.pub' in quickstart
    install_script = (output_dir / "install.sh").read_text(encoding="utf-8")
    assert "stale-install" not in install_script
    copied_key = (output_dir / ssh_pub.name).read_text(encoding="utf-8")
    assert copied_key == "ssh-ed25519 AAAATEST quickhatch\n"
    handoff = json.loads((output_dir / "quickhatch-handoff.json").read_text(encoding="utf-8"))
    assert handoff["type"] == "quickhatch-handoff"
    assert handoff["profile"]["ai_config"]["api_key"] is None
    assert [s["category"] for s in handoff["bridge"]["suggestions"]] == [
        "install_script",
        "plan",
    ]
    assert [m["content"] for m in handoff["bridge"]["messages"]] == ["analysis summary"]

    zip_path = output_dir / "quickhatch-migration-bundle.zip"
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert "START_HERE.md" in names
    assert "quickhatch-handoff.json" in names
    assert "quickstart.sh" in names
    assert "install.sh" in names
    assert ssh_pub.name in names
    assert "old-key.pub" not in names


def test_run_export_rejects_stale_saved_ssh_key(monkeypatch, tmp_path):
    profile = UserProfile()
    profile.preferences.another_machine = "yes"
    profile.ssh_public_key_path = str(tmp_path / "missing" / "quickhatch_ed25519.pub")

    bridge = BridgeState(profile, persist_path=None)
    gui_state = GUIState(profile, bridge)
    output_dir = tmp_path / "export"

    monkeypatch.setattr(gui_server_mod, "_export_output_dir", lambda: output_dir)

    result = _run_export(gui_state)

    assert result["status"] == "error"
    assert "Saved SSH key path points to files that no longer exist" in result["message"]
    assert result["ssh_key_status"]["ready"] is False
    assert not (output_dir / "quickstart.sh").exists()
