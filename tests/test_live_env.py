"""Tests for live-USB detection + reconnect-after-reboot infrastructure."""

from __future__ import annotations

from unittest.mock import patch

from quickhatch.gui.server import (
    _detect_live_env,
    _is_reboot_command,
    _wait_for_installed_ssh_reconnect,
    _wait_for_ssh_reconnect,
)


class TestIsRebootCommand:
    def test_plain_reboot(self):
        assert _is_reboot_command("reboot")
        assert _is_reboot_command("sudo reboot")
        assert _is_reboot_command("reboot now")

    def test_shutdown_variants(self):
        assert _is_reboot_command("shutdown -r now")
        assert _is_reboot_command("sudo shutdown -r 0")
        assert _is_reboot_command("systemctl reboot")
        assert _is_reboot_command("init 6")
        assert _is_reboot_command("telinit 6")

    def test_reboot_mid_command(self):
        assert _is_reboot_command("umount -R /mnt && reboot")
        assert _is_reboot_command("echo done; reboot")
        assert _is_reboot_command("partprobe /dev/sda || reboot")

    def test_non_reboot_commands(self):
        assert not _is_reboot_command("ls -la")
        assert not _is_reboot_command("pacman -S reboot-utils")  # substring false match
        assert not _is_reboot_command('echo "reboot is scary"')
        assert not _is_reboot_command("# reboot would be done here")
        assert not _is_reboot_command("")


class TestDetectLiveEnv:
    @staticmethod
    def _fake_ssh(stdout: str, rc: int = 0):
        return lambda host, user, cmd, key, timeout=120, port=22: (rc, stdout, "")

    def test_detects_cmdline_signal(self):
        with patch("quickhatch.gui.server._ssh_exec", self._fake_ssh("LIVE:cmdline\n")):
            is_live, marker = _detect_live_env("1.2.3.4", "liveuser", None)
        assert is_live
        assert marker == "LIVE:cmdline"

    def test_detects_marker_dir(self):
        with patch(
            "quickhatch.gui.server._ssh_exec",
            self._fake_ssh("LIVE:marker=/cdrom/casper\n"),
        ):
            is_live, marker = _detect_live_env("1.2.3.4", "ubuntu", None)
        assert is_live
        assert "marker" in marker

    def test_detects_overlay_root(self):
        with patch("quickhatch.gui.server._ssh_exec", self._fake_ssh("LIVE:overlay-root\n")):
            is_live, marker = _detect_live_env("1.2.3.4", "root", None)
        assert is_live

    def test_installed_system_returns_false(self):
        with patch("quickhatch.gui.server._ssh_exec", self._fake_ssh("INSTALLED\n")):
            is_live, marker = _detect_live_env("1.2.3.4", "jan", None)
        assert not is_live
        assert marker == "INSTALLED"

    def test_ssh_failure_returns_false(self):
        with patch("quickhatch.gui.server._ssh_exec", self._fake_ssh("", rc=255)):
            is_live, marker = _detect_live_env("1.2.3.4", "jan", None)
        assert not is_live
        assert marker == "detect-failed"


class TestWaitForSshReconnect:
    def test_returns_true_on_immediate_success(self):
        with patch(
            "quickhatch.gui.server._ssh_exec",
            return_value=(0, "__reconnected__\n", ""),
        ):
            ok = _wait_for_ssh_reconnect("1.2.3.4", "jan", None, timeout=5, interval=1)
        assert ok is True

    def test_returns_true_after_retries(self):
        # First 2 calls fail (-1), third succeeds
        calls = iter([(-1, "", "timeout"), (-1, "", "timeout"), (0, "__reconnected__", "")])
        with patch(
            "quickhatch.gui.server._ssh_exec",
            side_effect=lambda *a, **kw: next(calls),
        ):
            ok = _wait_for_ssh_reconnect("1.2.3.4", "jan", None, timeout=30, interval=0)
        assert ok is True

    def test_returns_false_on_timeout(self):
        with patch("quickhatch.gui.server._ssh_exec", return_value=(-1, "", "timeout")):
            ok = _wait_for_ssh_reconnect("1.2.3.4", "jan", None, timeout=2, interval=1)
        assert ok is False


class TestWaitForInstalledSshReconnect:
    def test_returns_new_user_when_installed_system_accepts_key(self):
        def fake_ssh(_host, user, cmd, _key, timeout=120, port=22):
            assert user == "jan"
            assert cmd == "echo __reconnected__"
            return (0, "__reconnected__\n", "")

        with patch("quickhatch.gui.server._ssh_exec", side_effect=fake_ssh):
            ok, message, connected_user = _wait_for_installed_ssh_reconnect(
                "1.2.3.4",
                "jan",
                None,
                fallback_user="root",
                timeout=5,
                interval=0,
            )
        assert ok is True
        assert message == ""
        assert connected_user == "jan"

    def test_fails_fast_when_target_boots_back_into_live_media(self):
        calls: list[tuple[str, str]] = []

        def fake_ssh(_host, user, cmd, _key, timeout=120, port=22):
            calls.append((user, cmd))
            if user == "jan":
                return (255, "", "Permission denied")
            assert user == "root"
            return (0, "LIVE:marker=/run/archiso\n", "")

        with patch("quickhatch.gui.server._ssh_exec", side_effect=fake_ssh):
            ok, message, connected_user = _wait_for_installed_ssh_reconnect(
                "1.2.3.4",
                "jan",
                None,
                fallback_user="root",
                timeout=30,
                interval=0,
            )
        assert ok is False
        assert connected_user is None
        assert "still booted into live media" in message
        assert calls[0] == ("jan", "echo __reconnected__")
        assert calls[1][0] == "root"
        assert "/run/archiso" in calls[1][1]

    def test_can_continue_with_fallback_user_if_installed_system_allows_it(self):
        def fake_ssh(_host, user, cmd, _key, timeout=120, port=22):
            if user == "jan":
                return (255, "", "Permission denied")
            assert user == "root"
            return (0, "INSTALLED\n", "")

        with patch("quickhatch.gui.server._ssh_exec", side_effect=fake_ssh):
            ok, message, connected_user = _wait_for_installed_ssh_reconnect(
                "1.2.3.4",
                "jan",
                None,
                fallback_user="root",
                timeout=30,
                interval=0,
            )
        assert ok is True
        assert message == ""
        assert connected_user == "root"

    def test_returns_false_when_stop_requested(self):
        stop = [False]
        call_count = [0]

        def fake_ssh(*a, **kw):
            call_count[0] += 1
            if call_count[0] >= 2:
                stop[0] = True
            return (-1, "", "timeout")

        with patch("quickhatch.gui.server._ssh_exec", side_effect=fake_ssh):
            ok = _wait_for_ssh_reconnect(
                "1.2.3.4",
                "jan",
                None,
                timeout=30,
                interval=0,
                stop_flag_getter=lambda: stop[0],
            )
        assert ok is False
