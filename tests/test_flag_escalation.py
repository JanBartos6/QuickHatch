"""Tests for the flag-escalation loop detector.

Motivated by a live run where the agent cycled through 8 variants of the
same intent (install nodejs-lts-iron + openai-codex) with progressively
more destructive flags: --noconfirm → --nodeps → -Rdd → piped `y`. The
same-exact-command detector missed it because each literal string
differed. The intent-signature normaliser reveals all variants as one.
"""

from __future__ import annotations

from quickhatch.gui.server import (
    _command_intent_signature,
    _detect_flag_escalation,
)


class TestCommandIntentSignature:
    def test_simple_install_signature(self):
        sig = _command_intent_signature("pacman -S steam")
        assert sig == "pacman:install:steam"

    def test_sudo_prefix_is_stripped(self):
        assert _command_intent_signature("sudo pacman -S steam") == "pacman:install:steam"

    def test_env_prefix_is_stripped(self):
        sig = _command_intent_signature("DEBIAN_FRONTEND=noninteractive sudo apt install foo")
        assert sig == "apt:install:foo"

    def test_flag_variants_collapse_to_same_signature(self):
        variants = [
            "sudo pacman -S nodejs-lts-iron openai-codex",
            "sudo pacman -S --noconfirm nodejs-lts-iron openai-codex",
            "sudo pacman -S --nodeps nodejs-lts-iron openai-codex",
            "sudo pacman -S --noconfirm --nodeps nodejs-lts-iron openai-codex",
        ]
        sigs = {_command_intent_signature(v) for v in variants}
        assert len(sigs) == 1
        assert "nodejs-lts-iron" in next(iter(sigs))
        assert "openai-codex" in next(iter(sigs))

    def test_piped_y_prefix_is_stripped(self):
        assert _command_intent_signature(
            "echo 'y' | sudo pacman -S --noconfirm nodejs-lts-iron"
        ) == _command_intent_signature("sudo pacman -S nodejs-lts-iron")

    def test_chain_takes_last_install_segment(self):
        # `pacman -Rdd nodejs && pacman -S nodejs-lts-iron openai-codex`
        # — the real intent is the final install, not the cleanup remove.
        sig = _command_intent_signature(
            "sudo pacman -Rdd --noconfirm nodejs && "
            "sudo pacman -S --noconfirm nodejs-lts-iron openai-codex"
        )
        assert sig == "pacman:install:nodejs-lts-iron openai-codex"

    def test_package_order_doesnt_matter(self):
        a = _command_intent_signature("pacman -S foo bar")
        b = _command_intent_signature("pacman -S bar foo")
        assert a == b

    def test_non_package_command_returns_none(self):
        assert _command_intent_signature("lsblk -f") is None
        assert _command_intent_signature("systemctl enable sshd") is None
        assert _command_intent_signature("") is None

    def test_remove_op_recognised(self):
        assert _command_intent_signature("sudo pacman -Rns nodejs") == "pacman:remove:nodejs"
        assert _command_intent_signature("sudo pacman -Rdd nodejs") == "pacman:remove:nodejs"

    def test_apt_and_dnf_supported(self):
        assert _command_intent_signature("apt install foo") == "apt:install:foo"
        assert _command_intent_signature("dnf install foo") == "dnf:install:foo"


class TestDetectFlagEscalation:
    def test_three_variants_of_same_intent_fires(self):
        history = [
            "sudo pacman -S --noconfirm nodejs-lts-iron openai-codex",
            "echo 'y' | sudo pacman -S --noconfirm nodejs-lts-iron openai-codex",
            "sudo pacman -S --noconfirm --nodeps nodejs-lts-iron openai-codex",
        ]
        sig = _detect_flag_escalation(history)
        assert sig is not None
        assert "nodejs-lts-iron" in sig

    def test_two_variants_does_not_fire(self):
        # Normal retry is fine — it's only when the pattern clearly
        # escalates that we intervene.
        history = [
            "sudo pacman -S steam",
            "sudo pacman -S --noconfirm steam",
        ]
        assert _detect_flag_escalation(history) is None

    def test_different_intents_does_not_fire(self):
        history = [
            "sudo pacman -S steam",
            "sudo pacman -S brave",
            "sudo pacman -S discord",
        ]
        assert _detect_flag_escalation(history) is None

    def test_mixed_pm_commands_fires_on_repeat_only(self):
        history = [
            "lsblk -f",
            "sudo pacman -S --noconfirm nodejs-lts-iron openai-codex",
            "sudo pacman -S --nodeps nodejs-lts-iron openai-codex",
            "echo 'y' | sudo pacman -S nodejs-lts-iron openai-codex",
            "systemctl is-enabled sshd",
        ]
        sig = _detect_flag_escalation(history)
        assert sig is not None
        assert "nodejs-lts-iron" in sig

    def test_the_live_run_pattern(self):
        # Reproduction of the exact sequence from the live bug report.
        history = [
            "sudo pacman -Rns --noconfirm nodejs && sudo pacman -S --noconfirm openai-codex && yay -S --noconfirm aione-bin",
            "sudo pacman -S --noconfirm nodejs-lts-iron openai-codex && yay -S --noconfirm aione-bin",
            "echo 'y' | sudo pacman -S --noconfirm nodejs-lts-iron openai-codex && yay -S --noconfirm aione-bin",
            "sudo pacman -S --noconfirm --nodeps nodejs-lts-iron && sudo pacman -S --noconfirm openai-codex && yay -S --noconfirm aione-bin",
            "sudo pacman -Rdd --noconfirm nodejs && sudo pacman -S --noconfirm nodejs-lts-iron openai-codex && yay -S --noconfirm aione-bin",
        ]
        sig = _detect_flag_escalation(history)
        assert sig is not None
        assert "nodejs-lts-iron" in sig
        assert "openai-codex" in sig


class TestStallVocabExtension:
    """Stall detector should now catch pacman-conflict class loops."""

    def test_nodejs_stall_caught(self):
        from quickhatch.gui.server import _detect_stall_topic

        reasons = [
            "resolving nodejs conflict with openai-codex",
            "forcing nodejs-lts-iron over nodejs",
            "retrying nodejs install with --nodeps",
            "nodejs dependency mismatch — trying -Rdd",
            "nodejs removal then reinstall",
        ]
        topic = _detect_stall_topic(reasons)
        assert topic in {"nodejs", "dependency", "deps", "conflict"}

    def test_conflict_stall_caught(self):
        from quickhatch.gui.server import _detect_stall_topic

        reasons = [
            "handling the pacman conflict",
            "resolving the conflict with --nodeps",
            "unresolvable dependency conflict",
            "another conflict — trying remove",
        ]
        topic = _detect_stall_topic(reasons)
        assert topic == "conflict"
