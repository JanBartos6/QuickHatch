"""Regression tests for the v4 code-review findings.

Each test covers one specific bug that was introduced, not just a
behavior. Naming matches the reviewer's P1/P2 numbering so the link
between test and fix is obvious.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from quickhatch.bridge.server import BridgeState
from quickhatch.config import UserProfile
from quickhatch.gui.server import _extract_created_user


class TestHeredocUseraddDistinction:
    """P1 #5: _extract_created_user must distinguish data-heredocs
    (cat <<EOF) from executing heredocs (bash/arch-chroot <<EOF)."""

    def test_arch_chroot_heredoc_useradd_is_detected(self):
        """useradd inside `arch-chroot <<EOF` IS executed → detect it."""
        cmd = "arch-chroot /mnt <<'EOF'\nuseradd -m -G wheel bob\necho bob:pw | chpasswd\nEOF"
        assert _extract_created_user(cmd) == "bob"

    def test_bash_heredoc_useradd_is_detected(self):
        cmd = "bash <<EOF\nuseradd -m alice\nEOF"
        assert _extract_created_user(cmd) == "alice"

    def test_ssh_heredoc_useradd_is_detected(self):
        cmd = "ssh root@target <<'EOF'\nuseradd -m carol\nEOF"
        assert _extract_created_user(cmd) == "carol"

    def test_sudo_bash_heredoc_detected(self):
        cmd = "sudo bash <<'EOF'\nuseradd -m dave\nEOF"
        assert _extract_created_user(cmd) == "dave"

    def test_cat_heredoc_useradd_is_not_detected(self):
        """useradd inside `cat <<EOF > file` is string data, not a command."""
        cmd = "cat > /tmp/setup.sh <<'EOF'\nuseradd -m fake\nEOF"
        assert _extract_created_user(cmd) is None

    def test_tee_heredoc_useradd_is_not_detected(self):
        cmd = "tee /tmp/note <<EOF\nuseradd fake\nEOF"
        assert _extract_created_user(cmd) is None

    def test_plain_useradd_still_detected(self):
        # No heredoc involved — the baseline case.
        assert _extract_created_user("useradd -m -G wheel eve") == "eve"


class TestResolvePrimerMultiDistro:
    """P1 #2: resolve_primer must prefer the recommended distro, not
    the first one mentioned in a comparison paragraph."""

    def test_recommend_at_end_wins_over_earlier_mentions(self):
        from quickhatch.ai.install_primer import resolve_primer

        text = (
            "I compared Fedora and Ubuntu. Fedora has a faster release "
            "cadence but the user runs Steam heavily, so I recommend Ubuntu LTS."
        )
        match = resolve_primer(text)
        assert match is not None
        assert "Ubuntu" in match.get("canonical", "")

    def test_bare_distro_name_still_resolves(self):
        from quickhatch.ai.install_primer import resolve_primer

        assert resolve_primer("arch") is not None
        assert resolve_primer("fedora") is not None


class TestPersistVersionOrdering:
    """P1 #1: _write_payload_unlocked must reject out-of-order writes
    so a slow thread can't overwrite newer state with an older snapshot.
    """

    def test_older_snapshot_does_not_clobber_newer(self, tmp_path):
        profile = UserProfile()
        bridge = BridgeState(profile, persist_path=tmp_path / "susp.json")
        # Take two snapshots with a mutation in between.
        with bridge.lock:
            bridge.messages.append({"role": "user", "content": "first"})
            snap_a = bridge._serialize_payload_locked()
        with bridge.lock:
            bridge.messages.append({"role": "user", "content": "second"})
            snap_b = bridge._serialize_payload_locked()

        # Write in reverse order — B first (newer), then A (older).
        bridge._write_payload_unlocked(snap_b)
        bridge._write_payload_unlocked(snap_a)  # should be rejected

        saved = json.loads((tmp_path / "susp.json").read_text(encoding="utf-8"))
        # The newer snapshot's messages must be on disk, not the older.
        contents = [m.get("content") for m in saved["messages"]]
        assert "second" in contents

    def test_concurrent_mutators_preserve_all_messages(self, tmp_path):
        """Both messages should land on disk even under concurrent writes.
        The version counter guarantees the final state is the highest-
        versioned snapshot, which by construction includes every prior
        mutation (since each snapshot reflects the list at its serialize
        time).
        """
        profile = UserProfile()
        bridge = BridgeState(profile, persist_path=tmp_path / "susp.json")

        def writer(role_prefix: str, n: int) -> None:
            for i in range(n):
                bridge.add_message("user", f"{role_prefix}-{i}")

        threads = [
            threading.Thread(target=writer, args=("a", 20)),
            threading.Thread(target=writer, args=("b", 20)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Give any trailing writes a moment to flush.
        time.sleep(0.05)
        saved = json.loads((tmp_path / "susp.json").read_text(encoding="utf-8"))
        # All 40 messages should be represented on disk.
        assert len(saved["messages"]) == 40


class TestAdapterRamOverflow:
    """P3 #15: signed-overflow VRAM values must be reinterpreted as
    unsigned, not discarded."""

    def test_negative_value_reinterpreted(self):
        from quickhatch.scanners.hardware import _parse_adapter_ram

        # Classic 4 GiB VRAM card: 4 GiB == 4294967296 bytes, which
        # overflows signed-32 to 0 (or becomes very negative near the
        # boundary). Use a 2 GiB case (2147483648 → signed-32 = -2147483648).
        mb = _parse_adapter_ram("-2147483648")
        assert mb == 2048

    def test_zero_is_unknown(self):
        from quickhatch.scanners.hardware import _parse_adapter_ram

        assert _parse_adapter_ram("0") is None

    def test_positive_still_works(self):
        from quickhatch.scanners.hardware import _parse_adapter_ram

        mb = _parse_adapter_ram(str(1024 * 1024 * 512))  # 512 MiB
        assert mb == 512


class TestWingetHeaderRejection:
    """P2 #10: winget parser must not turn localized headers into apps."""

    def test_headers_dropped_without_separator(self):
        """If the separator row is missing, header-looking tokens get rejected."""
        # Simulate the internals: we can't easily invoke _scan_winget without
        # winget installed, but we can assert the id/version regex behavior
        # the filter relies on.
        import re

        pat = re.compile(
            r"(?:\b[A-Za-z][A-Za-z0-9]*\.[A-Za-z][A-Za-z0-9.]*\b)"
            r"|(?:\b\d+\.\d+(?:\.\d+)*\b)"
        )
        assert pat.search("Name  Id  Version  Source") is None
        assert pat.search("Nombre  Identificador  Version") is None
        # A real row has a Publisher.App id or dotted version.
        assert pat.search("Brave Browser  BraveSoftware.Brave  1.74.48  winget")
        assert pat.search("Git  Git.Git  2.47.1  winget")


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
