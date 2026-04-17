"""Tests for the VM-verify module.

Every external tool (VBoxManage, qemu, ssh, bash) is monkeypatched so
the tests run on a laptop with none of them installed.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from quickhatch.wizard import vm_verify

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_completed(rc: int = 0, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# detect_backends / pick_backend
# ---------------------------------------------------------------------------


class TestBackendDetection:
    def test_none_available(self, monkeypatch):
        monkeypatch.setattr(vm_verify.shutil, "which", lambda _n: None)
        monkeypatch.setattr(vm_verify.Path, "exists", lambda _s: False)
        assert vm_verify.pick_backend() == ""

    def test_virtualbox_preferred_when_both(self, monkeypatch):
        def _which(n):
            return {"VBoxManage": "/usr/bin/VBoxManage", "qemu-system-x86_64": "/usr/bin/qemu"}.get(
                n
            )

        monkeypatch.setattr(vm_verify.shutil, "which", _which)
        assert vm_verify.pick_backend() == "virtualbox"

    def test_qemu_fallback(self, monkeypatch):
        def _which(n):
            return "/usr/bin/qemu" if n == "qemu-system-x86_64" else None

        monkeypatch.setattr(vm_verify.shutil, "which", _which)
        monkeypatch.setattr(vm_verify.Path, "exists", lambda _s: False)
        assert vm_verify.pick_backend() == "qemu"

    def test_virtualbox_env_var_nonstandard_install_dir(self, monkeypatch, tmp_path):
        # Simulate a VirtualBox installed on G:\VirtualBox (not the default
        # Program Files location) — the installer sets VBOX_MSI_INSTALL_PATH.
        fake_install = tmp_path / "VBoxOnDriveG"
        fake_install.mkdir()
        (fake_install / "VBoxManage.exe").write_text("fake")

        monkeypatch.setattr(vm_verify.shutil, "which", lambda _n: None)
        monkeypatch.setenv("VBOX_MSI_INSTALL_PATH", str(fake_install))
        # Make sure the other env var doesn't interfere.
        monkeypatch.delenv("VBOX_INSTALL_PATH", raising=False)

        found = vm_verify.detect_backends()
        assert found["virtualbox"] is not None
        assert "VBoxManage.exe" in found["virtualbox"]

    def test_prefer_qemu_when_both(self, monkeypatch):
        def _which(n):
            return {"VBoxManage": "/usr/bin/VBoxManage", "qemu-system-x86_64": "/usr/bin/qemu"}.get(
                n
            )

        monkeypatch.setattr(vm_verify.shutil, "which", _which)
        assert vm_verify.pick_backend(prefer="qemu") == "qemu"


# ---------------------------------------------------------------------------
# bash_syntax_check
# ---------------------------------------------------------------------------


class TestBashSyntaxCheck:
    def test_no_bash_is_soft_pass(self, monkeypatch, tmp_path):
        script = tmp_path / "install.sh"
        script.write_text("echo hi")
        monkeypatch.setattr(vm_verify.shutil, "which", lambda _n: None)
        ok, msg = vm_verify.bash_syntax_check(script)
        assert ok
        assert "skipped" in msg

    def test_bash_passes(self, monkeypatch, tmp_path):
        script = tmp_path / "install.sh"
        script.write_text("echo ok")
        monkeypatch.setattr(vm_verify.shutil, "which", lambda _n: "/bin/bash")
        monkeypatch.setattr(vm_verify, "_run", lambda *a, **k: _mk_completed(0))
        ok, msg = vm_verify.bash_syntax_check(script)
        assert ok
        assert "passed" in msg

    def test_bash_fails(self, monkeypatch, tmp_path):
        script = tmp_path / "install.sh"
        script.write_text("if then")
        monkeypatch.setattr(vm_verify.shutil, "which", lambda _n: "/bin/bash")
        monkeypatch.setattr(
            vm_verify, "_run", lambda *a, **k: _mk_completed(2, stderr="syntax error near then")
        )
        ok, msg = vm_verify.bash_syntax_check(script)
        assert not ok
        assert "syntax error" in msg


# ---------------------------------------------------------------------------
# VirtualBoxBackend — no real VM, just command construction
# ---------------------------------------------------------------------------


class TestVirtualBoxBackend:
    def test_create_issues_expected_commands(self, monkeypatch, tmp_path):
        calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _mk_completed(0)

        monkeypatch.setattr(vm_verify, "_run", _fake_run)
        iso = tmp_path / "dummy.iso"
        iso.write_text("x")
        vm = vm_verify.VirtualBoxBackend("VBoxManage")
        vm.create(iso)
        joined = [" ".join(c) for c in calls]
        assert any("createvm" in c for c in joined)
        assert any("natpf1" in c and "22022" in c for c in joined)
        assert any(str(iso) in c for c in joined)

    def test_teardown_swallows_errors(self, monkeypatch):
        def _fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 5)

        monkeypatch.setattr(vm_verify, "_run", _fake_run)
        monkeypatch.setattr(vm_verify.time, "sleep", lambda _s: None)
        vm = vm_verify.VirtualBoxBackend("VBoxManage")
        vm.teardown()  # must not raise


# ---------------------------------------------------------------------------
# QemuBackend
# ---------------------------------------------------------------------------


class TestQemuBackend:
    def test_create_invokes_qemu_img(self, monkeypatch, tmp_path):
        calls: list[list[str]] = []
        monkeypatch.setattr(
            vm_verify.shutil,
            "which",
            lambda n: "/usr/bin/qemu-img" if n == "qemu-img" else None,
        )
        monkeypatch.setattr(
            vm_verify, "_run", lambda cmd, **k: calls.append(cmd) or _mk_completed(0)
        )
        iso = tmp_path / "live.iso"
        iso.write_text("x")
        vm = vm_verify.QemuBackend("qemu-system-x86_64")
        vm.create(iso)
        assert any("create" in c for c in calls[0])
        assert vm.disk_path is not None


# ---------------------------------------------------------------------------
# verify_install_script end-to-end — mocked
# ---------------------------------------------------------------------------


class TestVerifyInstallScript:
    def test_syntax_only_when_no_iso(self, monkeypatch, tmp_path):
        script = tmp_path / "install.sh"
        script.write_text("echo ok")
        monkeypatch.setattr(vm_verify, "bash_syntax_check", lambda _p: (True, "ok"))
        result = vm_verify.verify_install_script(script, iso_path=None)
        assert result.success
        assert result.backend == "syntax-only"

    def test_syntax_fail_blocks(self, monkeypatch, tmp_path):
        script = tmp_path / "install.sh"
        script.write_text("bad")
        monkeypatch.setattr(vm_verify, "bash_syntax_check", lambda _p: (False, "boom"))
        result = vm_verify.verify_install_script(script, iso_path=tmp_path / "x.iso")
        assert not result.success
        assert "bash -n failed" in result.error

    def test_no_backend_fails_cleanly(self, monkeypatch, tmp_path):
        script = tmp_path / "install.sh"
        script.write_text("echo ok")
        iso = tmp_path / "x.iso"
        iso.write_text("x")
        monkeypatch.setattr(vm_verify, "bash_syntax_check", lambda _p: (True, "ok"))
        monkeypatch.setattr(vm_verify, "pick_backend", lambda prefer=None: "")
        result = vm_verify.verify_install_script(script, iso_path=iso)
        assert not result.success
        assert "No VM backend" in result.error

    def test_happy_path_with_mocked_backend(self, monkeypatch, tmp_path):
        script = tmp_path / "install.sh"
        script.write_text("echo ok")
        iso = tmp_path / "live.iso"
        iso.write_text("x")

        monkeypatch.setattr(vm_verify, "bash_syntax_check", lambda _p: (True, "ok"))
        monkeypatch.setattr(vm_verify, "pick_backend", lambda prefer=None: "virtualbox")
        monkeypatch.setattr(
            vm_verify,
            "detect_backends",
            lambda: {"virtualbox": "VBoxManage", "qemu": None},
        )
        monkeypatch.setattr(vm_verify, "_wait_for_vm_ssh", lambda *a, **k: True)
        monkeypatch.setattr(vm_verify, "run_script_in_vm", lambda *a, **k: (0, "done", ""))

        class _FakeVM:
            vm_name = "fake"

            def __init__(self, *a, **k):
                pass

            def create(self, *a, **k):
                pass

            def start(self):
                pass

            def teardown(self):
                pass

        monkeypatch.setattr(vm_verify, "VirtualBoxBackend", _FakeVM)
        result = vm_verify.verify_install_script(script, iso_path=iso)
        assert result.success
        assert result.exit_code == 0
        assert "vm-started" in result.stages
        assert "ssh-up" in result.stages

    def test_soft_pass_exit_code(self, monkeypatch, tmp_path):
        script = tmp_path / "install.sh"
        script.write_text("echo ok")
        iso = tmp_path / "live.iso"
        iso.write_text("x")
        monkeypatch.setattr(vm_verify, "bash_syntax_check", lambda _p: (True, "ok"))
        monkeypatch.setattr(vm_verify, "pick_backend", lambda prefer=None: "qemu")
        monkeypatch.setattr(
            vm_verify,
            "detect_backends",
            lambda: {"virtualbox": None, "qemu": "qemu-system-x86_64"},
        )
        monkeypatch.setattr(vm_verify, "_wait_for_vm_ssh", lambda *a, **k: True)
        monkeypatch.setattr(
            vm_verify,
            "run_script_in_vm",
            lambda *a, **k: (105, "skipped hw", ""),
        )

        class _FakeVM:
            vm_name = "fake"

            def __init__(self, *a, **k):
                pass

            def create(self, *a, **k):
                pass

            def start(self):
                pass

            def teardown(self):
                pass

        monkeypatch.setattr(vm_verify, "QemuBackend", _FakeVM)
        result = vm_verify.verify_install_script(script, iso_path=iso)
        assert result.success  # 105 is in soft-pass range
        assert result.exit_code == 105

    def test_ssh_never_comes_up(self, monkeypatch, tmp_path):
        script = tmp_path / "install.sh"
        script.write_text("echo ok")
        iso = tmp_path / "live.iso"
        iso.write_text("x")
        monkeypatch.setattr(vm_verify, "bash_syntax_check", lambda _p: (True, "ok"))
        monkeypatch.setattr(vm_verify, "pick_backend", lambda prefer=None: "virtualbox")
        monkeypatch.setattr(
            vm_verify,
            "detect_backends",
            lambda: {"virtualbox": "VBoxManage", "qemu": None},
        )
        monkeypatch.setattr(vm_verify, "_wait_for_vm_ssh", lambda *a, **k: False)

        class _FakeVM:
            vm_name = "fake"

            def __init__(self, *a, **k):
                pass

            def create(self, *a, **k):
                pass

            def start(self):
                pass

            def teardown(self):
                pass

        monkeypatch.setattr(vm_verify, "VirtualBoxBackend", _FakeVM)
        result = vm_verify.verify_install_script(script, iso_path=iso)
        assert not result.success
        assert "never answered" in result.error


# ---------------------------------------------------------------------------
# VM-aware prompt block
# ---------------------------------------------------------------------------


class TestVmAwarePromptBlock:
    def test_enabled_includes_guard_instructions(self):
        block = vm_verify.vm_aware_prompt_block(True)
        assert "QUICKHATCH_VM_VERIFY" in block
        assert "exit_code=105" in block
        assert "hardware" in block.lower()

    def test_disabled_returns_empty(self):
        assert vm_verify.vm_aware_prompt_block(False) == ""


# ---------------------------------------------------------------------------
# Integration with _build_section_prompt
# ---------------------------------------------------------------------------


class TestPromptIntegration:
    def test_install_script_section_picks_up_vm_block(self):
        from quickhatch.config import Preferences, UserProfile
        from quickhatch.gui.server import _build_section_prompt

        profile = UserProfile()
        profile.preferences = Preferences(another_machine="no", distro_preference="arch")
        prompt = _build_section_prompt(profile, "install_script", search_context="", vm_verify=True)
        assert "QUICKHATCH_VM_VERIFY" in prompt

    def test_install_script_section_no_vm_block_by_default(self):
        from quickhatch.config import Preferences, UserProfile
        from quickhatch.gui.server import _build_section_prompt

        profile = UserProfile()
        profile.preferences = Preferences(another_machine="no", distro_preference="arch")
        prompt = _build_section_prompt(
            profile, "install_script", search_context="", vm_verify=False
        )
        assert "QUICKHATCH_VM_VERIFY" not in prompt

    def test_non_install_section_ignores_vm_flag(self):
        from quickhatch.config import UserProfile
        from quickhatch.gui.server import _build_section_prompt

        profile = UserProfile()
        prompt = _build_section_prompt(profile, "plan", search_context="", vm_verify=True)
        assert "QUICKHATCH_VM_VERIFY" not in prompt


# ---------------------------------------------------------------------------
# VerifyResult.summary
# ---------------------------------------------------------------------------


class TestVerifyResultSummary:
    def test_success_summary(self):
        r = vm_verify.VerifyResult(success=True, backend="virtualbox", stages=["a", "b", "c"])
        assert "passed" in r.summary()
        assert "3 stages" in r.summary()

    def test_singular_stage_grammar(self):
        r = vm_verify.VerifyResult(success=True, backend="syntax-only", stages=["x"])
        assert "1 stage" in r.summary() and "1 stages" not in r.summary()

    def test_failure_with_error(self):
        r = vm_verify.VerifyResult(success=False, backend="qemu", error="boom")
        s = r.summary()
        assert "failed" in s and "boom" in s

    def test_failure_with_exit_code(self):
        r = vm_verify.VerifyResult(success=False, backend="qemu", exit_code=42)
        assert "exit=42" in r.summary()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
