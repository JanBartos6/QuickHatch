from __future__ import annotations

from quickhatch.setup.attempts import is_diagnostic_command


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
