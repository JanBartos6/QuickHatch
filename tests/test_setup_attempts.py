from __future__ import annotations

from quickhatch.setup.attempts import build_attempt_signature, is_diagnostic_command


def test_driver_runtime_probes_are_diagnostics() -> None:
    for command in (
        "nvidia-smi",
        "sudo nvidia-smi",
        "lsmod | grep nvidia",
        "modinfo nvidia",
        "sudo modinfo nvidia_drm",
        "vulkaninfo --summary",
    ):
        assert is_diagnostic_command(command) is True


def test_dnf5_search_is_a_diagnostic() -> None:
    assert is_diagnostic_command("dnf5 search akmod-nvidia") is True


def test_package_attempt_signature_ignores_sudo_wrapper() -> None:
    first = build_attempt_signature(
        stage="apps",
        group_id="obsidian",
        group_title="Install Obsidian",
        command="sudo apt-get install -y obsidian",
        stderr="E: Unable to locate package obsidian",
    )
    second = build_attempt_signature(
        stage="apps",
        group_id="obsidian",
        group_title="Install Obsidian",
        command="apt-get install --yes obsidian",
        stderr="E: Unable to locate package obsidian",
    )

    assert first.loop_key == second.loop_key
    assert first.packages == ("obsidian",)
