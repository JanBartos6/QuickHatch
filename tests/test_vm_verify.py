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

    def test_wsl_bash_uses_mnt_path_for_windows_absolute_path(self, monkeypatch):
        monkeypatch.setattr(vm_verify.os, "name", "nt")
        path = vm_verify._script_path_for_bash(
            r"G:\QuickHatch\.quickhatch\certification\arch\certify-install.sh",
            r"C:\Windows\System32\bash.exe",
        )
        assert path == "/mnt/g/QuickHatch/.quickhatch/certification/arch/certify-install.sh"


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

    def test_type_command_uses_layout_independent_scancodes(self, monkeypatch):
        calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _mk_completed(0)

        monkeypatch.setattr(vm_verify, "_run", _fake_run)
        monkeypatch.setattr(vm_verify.time, "sleep", lambda _s: None)
        vm = vm_verify.VirtualBoxBackend("VBoxManage")
        vm.type_command("echo ok >/tmp/qh")

        assert all("keyboardputstring" not in call for call in calls)
        joined = " ".join(" ".join(call) for call in calls)
        assert "keyboardputscancode" in joined
        assert "34" in joined  # `>` is Shift+period on a US keyboard.

    def test_type_command_keeps_shifted_scancodes_atomic_across_chunks(self, monkeypatch):
        calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _mk_completed(0)

        monkeypatch.setattr(vm_verify, "_run", _fake_run)
        monkeypatch.setattr(vm_verify.time, "sleep", lambda _s: None)
        vm = vm_verify.VirtualBoxBackend("VBoxManage")
        vm.type_command(("a" * 15) + ">")

        scancode_payloads = [call[4:] for call in calls if "keyboardputscancode" in call]
        assert ["2a", "34", "b4", "aa"] in scancode_payloads

    def test_press_enter_sends_enter_scancode(self, monkeypatch):
        calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _mk_completed(0)

        monkeypatch.setattr(vm_verify, "_run", _fake_run)
        vm = vm_verify.VirtualBoxBackend("VBoxManage")
        vm.press_enter()

        assert calls == [["VBoxManage", "controlvm", vm.vm_name, "keyboardputscancode", "1c", "9c"]]

    def test_switch_to_tty_sends_ctrl_alt_function_key(self, monkeypatch):
        calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _mk_completed(0)

        monkeypatch.setattr(vm_verify, "_run", _fake_run)
        vm = vm_verify.VirtualBoxBackend("VBoxManage")
        vm.switch_to_tty(3)

        assert calls == [
            [
                "VBoxManage",
                "controlvm",
                vm.vm_name,
                "keyboardputscancode",
                "1d",
                "38",
                "3d",
                "bd",
                "b8",
                "9d",
            ]
        ]


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
# run_script_in_vm
# ---------------------------------------------------------------------------


class TestRunScriptInVm:
    def test_retries_legacy_scp_when_sftp_subsystem_is_missing(
        self,
        monkeypatch,
        tmp_path,
    ):
        script = tmp_path / "install.sh"
        script.write_text("echo ok", encoding="utf-8")
        calls: list[list[str]] = []
        progress: list[str] = []

        def _fake_which(name):
            return name if name in {"ssh", "scp"} else None

        def _fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[0] == "scp" and "-O" not in cmd:
                return _mk_completed(
                    255,
                    stderr="subsystem request failed on channel 0\nscp: Connection closed",
                )
            return _mk_completed(0, stdout="done")

        monkeypatch.setattr(vm_verify.shutil, "which", _fake_which)
        monkeypatch.setattr(vm_verify, "_run", _fake_run)

        rc, out, err = vm_verify.run_script_in_vm(
            script,
            "root",
            22022,
            "key",
            progress=progress.append,
        )

        scp_calls = [call for call in calls if call[0] == "scp"]
        assert rc == 0
        assert out == "done"
        assert err == ""
        assert len(scp_calls) == 2
        assert "-O" not in scp_calls[0]
        assert "-O" in scp_calls[1]
        assert any("legacy scp mode" in message for message in progress)


class TestSshKeyAuthWait:
    def test_waits_for_key_auth_after_ssh_port_opens(self, monkeypatch):
        attempts = [False, False, True]
        now = [0.0]

        def _fake_ready(*args, **kwargs):
            return attempts.pop(0)

        def _fake_sleep(seconds):
            now[0] += seconds

        monkeypatch.setattr(vm_verify, "_vm_ssh_key_auth_ready", _fake_ready)
        monkeypatch.setattr(vm_verify.time, "time", lambda: now[0])
        monkeypatch.setattr(vm_verify.time, "sleep", _fake_sleep)

        assert vm_verify._wait_for_vm_ssh_key_auth(
            "user",
            22022,
            "key",
            timeout=20,
            interval=5,
        )
        assert now[0] == 10
        assert attempts == []


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

    def test_live_console_bootstrap_when_initial_ssh_missing(self, monkeypatch, tmp_path):
        script = tmp_path / "install.sh"
        script.write_text("echo ok")
        iso = tmp_path / "live.iso"
        iso.write_text("x")
        typed = []
        waits = []

        monkeypatch.setattr(vm_verify, "bash_syntax_check", lambda _p: (True, "ok"))
        monkeypatch.setattr(vm_verify, "pick_backend", lambda prefer=None: "virtualbox")
        monkeypatch.setattr(
            vm_verify,
            "detect_backends",
            lambda: {"virtualbox": "VBoxManage", "qemu": None},
        )

        def _fake_wait(*args, **kwargs):
            waits.append(kwargs.get("timeout"))
            return len(waits) > 1

        monkeypatch.setattr(vm_verify, "_wait_for_vm_ssh", _fake_wait)
        monkeypatch.setattr(vm_verify, "run_script_in_vm", lambda *a, **k: (0, "done", ""))
        monkeypatch.setattr(vm_verify.time, "sleep", lambda _s: None)

        class _FakeVM:
            vm_name = "fake"

            def __init__(self, *a, **k):
                pass

            def create(self, *a, **k):
                pass

            def start(self):
                pass

            def press_enter(self):
                pass

            def type_command(self, command):
                typed.append(command)

            def teardown(self):
                pass

        monkeypatch.setattr(vm_verify, "VirtualBoxBackend", _FakeVM)
        result = vm_verify.verify_install_script(
            script,
            iso_path=iso,
            live_bootstrap_command="systemctl start sshd",
            live_bootstrap_wait_seconds=0,
        )
        assert result.success
        assert typed == ["systemctl start sshd"]
        assert waits == [60, 600]

    def test_live_console_bootstrap_can_login_to_graphical_live_tty(self, monkeypatch, tmp_path):
        script = tmp_path / "install.sh"
        script.write_text("echo ok")
        iso = tmp_path / "live.iso"
        iso.write_text("x")
        typed = []
        tty_switches = []
        waits = []

        monkeypatch.setattr(vm_verify, "bash_syntax_check", lambda _p: (True, "ok"))
        monkeypatch.setattr(vm_verify, "pick_backend", lambda prefer=None: "virtualbox")
        monkeypatch.setattr(
            vm_verify,
            "detect_backends",
            lambda: {"virtualbox": "VBoxManage", "qemu": None},
        )

        def _fake_wait(*args, **kwargs):
            waits.append(kwargs.get("timeout"))
            return len(waits) > 1

        monkeypatch.setattr(vm_verify, "_wait_for_vm_ssh", _fake_wait)
        monkeypatch.setattr(vm_verify, "run_script_in_vm", lambda *a, **k: (0, "done", ""))
        monkeypatch.setattr(vm_verify.time, "sleep", lambda _s: None)

        class _FakeVM:
            vm_name = "fake"

            def __init__(self, *a, **k):
                pass

            def create(self, *a, **k):
                pass

            def start(self):
                pass

            def press_enter(self):
                pass

            def switch_to_tty(self, tty):
                tty_switches.append(tty)

            def type_command(self, command):
                typed.append(command)

            def teardown(self):
                pass

        monkeypatch.setattr(vm_verify, "VirtualBoxBackend", _FakeVM)
        result = vm_verify.verify_install_script(
            script,
            iso_path=iso,
            live_bootstrap_command="systemctl start sshd",
            live_bootstrap_user="liveuser",
            live_bootstrap_wait_seconds=0,
        )

        assert result.success
        assert tty_switches == [3]
        assert typed == ["liveuser", "systemctl start sshd"]

    def test_live_console_key_seed_when_ssh_banner_is_already_up(self, monkeypatch, tmp_path):
        script = tmp_path / "install.sh"
        script.write_text("echo ok")
        iso = tmp_path / "live.iso"
        iso.write_text("x")
        typed = []
        waits = []

        monkeypatch.setattr(vm_verify, "bash_syntax_check", lambda _p: (True, "ok"))
        monkeypatch.setattr(vm_verify, "pick_backend", lambda prefer=None: "virtualbox")
        monkeypatch.setattr(
            vm_verify,
            "detect_backends",
            lambda: {"virtualbox": "VBoxManage", "qemu": None},
        )
        monkeypatch.setattr(
            vm_verify,
            "_wait_for_vm_ssh",
            lambda *a, **k: waits.append(k.get("timeout")) or True,
        )
        monkeypatch.setattr(vm_verify, "run_script_in_vm", lambda *a, **k: (0, "done", ""))
        monkeypatch.setattr(vm_verify.time, "sleep", lambda _s: None)

        class _FakeVM:
            vm_name = "fake"

            def __init__(self, *a, **k):
                pass

            def create(self, *a, **k):
                pass

            def start(self):
                pass

            def press_enter(self):
                pass

            def type_command(self, command):
                typed.append(command)

            def teardown(self):
                pass

        monkeypatch.setattr(vm_verify, "VirtualBoxBackend", _FakeVM)
        result = vm_verify.verify_install_script(
            script,
            iso_path=iso,
            live_bootstrap_command="mkdir -p /root/.ssh",
        )
        assert result.success
        assert typed == ["mkdir -p /root/.ssh"]
        assert waits == [60]

    def test_full_post_reboot_path_with_mocked_backend(self, monkeypatch, tmp_path):
        script = tmp_path / "install.sh"
        script.write_text("echo ok")
        iso = tmp_path / "live.iso"
        iso.write_text("x")
        reboots = []
        audit_calls = []

        monkeypatch.setattr(vm_verify, "bash_syntax_check", lambda _p: (True, "ok"))
        monkeypatch.setattr(vm_verify, "pick_backend", lambda prefer=None: "virtualbox")
        monkeypatch.setattr(
            vm_verify,
            "detect_backends",
            lambda: {"virtualbox": "VBoxManage", "qemu": None},
        )
        monkeypatch.setattr(vm_verify, "_wait_for_vm_ssh", lambda *a, **k: True)
        monkeypatch.setattr(vm_verify, "run_script_in_vm", lambda *a, **k: (0, "done", ""))

        def _fake_audit(command, *args, **kwargs):
            del args, kwargs
            audit_calls.append(command)
            return (0, "audit ok", "")

        monkeypatch.setattr(vm_verify, "run_command_in_vm", _fake_audit)

        class _FakeVM:
            vm_name = "fake"

            def __init__(self, *a, **k):
                pass

            def create(self, *a, **k):
                pass

            def start(self):
                pass

            def boot_from_disk(self):
                reboots.append(1)

            def teardown(self):
                pass

        monkeypatch.setattr(vm_verify, "VirtualBoxBackend", _FakeVM)
        result = vm_verify.verify_install_script(
            script,
            iso_path=iso,
            post_reboot_user="jan",
            post_reboot_commands=["test -s /etc/fstab"],
            require_post_reboot=True,
        )
        assert result.success
        assert reboots == [1]
        assert audit_calls == ["test -s /etc/fstab"]
        assert "post-reboot-ssh-up" in result.stages
        assert "post-reboot-audit:1:0" in result.stages

    def test_post_reboot_audit_failure_blocks(self, monkeypatch, tmp_path):
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
        monkeypatch.setattr(
            vm_verify,
            "run_command_in_vm",
            lambda *a, **k: (1, "", "missing fstab"),
        )

        class _FakeVM:
            vm_name = "fake"

            def __init__(self, *a, **k):
                pass

            def create(self, *a, **k):
                pass

            def start(self):
                pass

            def boot_from_disk(self):
                pass

            def teardown(self):
                pass

        monkeypatch.setattr(vm_verify, "VirtualBoxBackend", _FakeVM)
        result = vm_verify.verify_install_script(
            script,
            iso_path=iso,
            post_reboot_commands=["test -s /etc/fstab"],
            require_post_reboot=True,
        )
        assert not result.success
        assert "Post-reboot audit command failed" in result.error

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
            lambda *a, **k: (105, "[quickhatch-vm-soft-pass]\nskipped hw", ""),
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
        assert result.success
        assert result.exit_code == 105

    def test_package_manager_exit_100_is_not_soft_pass_without_marker(
        self,
        monkeypatch,
        tmp_path,
    ):
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
        monkeypatch.setattr(
            vm_verify,
            "run_script_in_vm",
            lambda *a, **k: (
                100,
                "E: Could not get lock /var/lib/dpkg/lock-frontend",
                "",
            ),
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

        monkeypatch.setattr(vm_verify, "VirtualBoxBackend", _FakeVM)
        result = vm_verify.verify_install_script(script, iso_path=iso)
        assert not result.success
        assert result.exit_code == 100
        assert "dpkg/lock-frontend" in result.error

    def test_vanilla_iso_auth_failure_gives_actionable_error(self, monkeypatch, tmp_path):
        """SSH answered but scp failed on auth — user needs ISO-prep guidance."""
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
        monkeypatch.setattr(
            vm_verify,
            "run_script_in_vm",
            lambda *a, **k: (255, "", "Permission denied (publickey,password)."),
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

        monkeypatch.setattr(vm_verify, "VirtualBoxBackend", _FakeVM)
        result = vm_verify.verify_install_script(script, iso_path=iso)
        assert not result.success
        # Error should mention the user's actionable options, not dump raw scp stderr
        assert "customized ISO" in result.error
        assert "authorized_keys" in result.error
        assert "Permission denied" not in result.error  # raw stderr suppressed

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
        assert "[quickhatch-vm-soft-pass]" in block
        assert "hardware" in block.lower()
        assert "Do not write a VM-only happy path" in block
        assert "assumes Arch/KDE/NVIDIA" in block

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


class TestPasteSnippet:
    def test_snippet_includes_pubkey(self):
        snippet = vm_verify.build_key_paste_snippet("ssh-ed25519 AAAA foo@bar")
        assert "ssh-ed25519 AAAA foo@bar" in snippet
        assert "/root/.ssh/authorized_keys" in snippet
        assert "$qh_home/.ssh/authorized_keys" in snippet
        assert "READY" in snippet

    def test_snippet_strips_newlines(self):
        snippet = vm_verify.build_key_paste_snippet("ssh-ed25519 AAAA\nfoo\r\n")
        assert "\n" not in snippet
        assert "ssh-ed25519 AAAAfoo" in snippet

    def test_snippet_covers_multiple_init_systems(self):
        s = vm_verify.build_key_paste_snippet("k")
        # At least systemctl + rc-service + service — covers systemd, OpenRC, sysvinit
        assert "systemctl start sshd" in s
        assert 'qh_rc="${qh_rc}-service"' in s
        assert '"$qh_rc" sshd start' in s
        assert 'qh_svc="${qh_svc}vice"' in s
        assert '"$qh_svc" ssh start' in s

    def test_snippet_falls_back_to_blank_password_sudo_for_live_isos(self):
        snippet = vm_verify.build_key_paste_snippet("k")
        assert "sudo -n true" in snippet
        assert "sudo -S sh -c" in snippet


class TestAssistedFlow:
    def _patch_for_success(self, monkeypatch, tmp_path):
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

        teardown_calls = []

        class _FakeVM:
            vm_name = "fake-assisted"

            def __init__(self, *a, **k):
                pass

            def create(self, *a, **k):
                pass

            def start(self, headless=True):
                pass

            def teardown(self):
                teardown_calls.append(1)

        monkeypatch.setattr(vm_verify, "VirtualBoxBackend", _FakeVM)
        return script, iso, teardown_calls

    def test_start_and_finish_happy_path(self, monkeypatch, tmp_path):
        script, iso, teardowns = self._patch_for_success(monkeypatch, tmp_path)
        monkeypatch.setattr(vm_verify, "_wait_for_vm_ssh", lambda *a, **k: True)
        monkeypatch.setattr(vm_verify, "run_script_in_vm", lambda *a, **k: (0, "done", ""))

        sess, err = vm_verify.start_assisted_session(
            script_path=script,
            iso_path=iso,
            public_key="ssh-ed25519 AAAA test",
        )
        assert err == ""
        assert sess is not None
        assert "ssh-ed25519 AAAA test" in sess.paste_snippet
        assert sess.vm_name == "fake-assisted"
        assert sess.ssh_user == "root"

        result = vm_verify.finish_assisted_session(sess)
        assert result.success
        assert result.exit_code == 0
        assert "vm-torn-down" in result.stages
        assert len(teardowns) == 1  # teardown always runs

    def test_start_fails_without_iso(self, monkeypatch, tmp_path):
        script = tmp_path / "install.sh"
        script.write_text("echo ok")
        iso = tmp_path / "missing.iso"  # does not exist
        sess, err = vm_verify.start_assisted_session(
            script_path=script, iso_path=iso, public_key="k"
        )
        assert sess is None
        assert "ISO not found" in err

    def test_finish_reports_ssh_timeout(self, monkeypatch, tmp_path):
        script, iso, teardowns = self._patch_for_success(monkeypatch, tmp_path)
        monkeypatch.setattr(vm_verify, "_wait_for_vm_ssh", lambda *a, **k: False)

        sess, err = vm_verify.start_assisted_session(
            script_path=script,
            iso_path=iso,
            public_key="k",
        )
        assert sess is not None
        result = vm_verify.finish_assisted_session(sess, ssh_timeout=1)
        assert not result.success
        assert "paste" in result.error.lower() or "ssh" in result.error.lower()
        # Teardown still called even on timeout
        assert len(teardowns) == 1

    def test_start_rejects_bad_script_syntax(self, monkeypatch, tmp_path):
        script = tmp_path / "install.sh"
        script.write_text("broken")
        iso = tmp_path / "live.iso"
        iso.write_text("x")
        monkeypatch.setattr(vm_verify, "bash_syntax_check", lambda _p: (False, "parse error"))
        sess, err = vm_verify.start_assisted_session(
            script_path=script, iso_path=iso, public_key="k"
        )
        assert sess is None
        assert "bash -n failed" in err


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
