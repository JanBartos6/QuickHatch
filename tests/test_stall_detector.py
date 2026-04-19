"""Tests for _detect_stall_topic — the guard that notices when the
setup agent has burned 4+ consecutive turns circling the same topic.
"""

from __future__ import annotations

import pytest

from quickhatch.gui.server import _detect_stall_topic


class TestStallDetector:
    def test_empty_does_not_fire(self):
        assert _detect_stall_topic([]) is None
        assert _detect_stall_topic(["one"]) is None
        assert _detect_stall_topic(["one", "two"]) is None

    def test_single_topic_loop_fires(self):
        reasons = [
            "Checking pacman.conf for misplaced Include",
            "Reading the mirrorlist section of pacman.conf",
            "Inspecting SigLevel in pacman.conf",
            "Reverting pacman.conf changes",
            "Re-checking pacman.conf",
        ]
        topic = _detect_stall_topic(reasons)
        assert topic == "pacman.conf"

    def test_mixed_topics_do_not_fire(self):
        reasons = [
            "Checking pacman.conf",
            "Enabling sddm service",
            "Installing yay",
            "Checking nvidia driver",
            "Listing AUR packages",
        ]
        assert _detect_stall_topic(reasons) is None

    def test_three_on_topic_not_enough(self):
        """Threshold is 4+, not 3+. Three mentions is suspicious but
        may still be legitimate investigation."""
        reasons = [
            "Checking mirrorlist",
            "Reading mirrorlist",
            "Inspecting mirrorlist contents",
            "Installing steam",
            "Enabling multilib",
        ]
        assert _detect_stall_topic(reasons) is None

    def test_clock_loop_fires(self):
        """The exact failure from the live run: Gemma circling the clock."""
        reasons = [
            "Checking system clock",
            "Running NTP sync to fix the clock",
            "The clock still looks wrong — retrying",
            "Re-verifying clock after NTP",
            "Inspecting clock again",
        ]
        topic = _detect_stall_topic(reasons)
        assert topic in ("clock", "date", "ntp", "timedatectl")

    def test_none_when_no_known_topic_word(self):
        """Reasons that don't hit any of our tracked topic words should
        not fire — we don't want random natural-language filler to
        trigger false positives."""
        reasons = [
            "Doing thing one",
            "Doing thing two",
            "Checking something",
            "Looking at output",
            "Running next step",
        ]
        assert _detect_stall_topic(reasons) is None

    def test_case_insensitive(self):
        reasons = [
            "Checking Pacman.Conf structure",
            "PACMAN.CONF needs a fix",
            "Editing pacman.conf",
            "Viewing pacman.conf again",
        ]
        topic = _detect_stall_topic(reasons)
        assert topic == "pacman.conf"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
