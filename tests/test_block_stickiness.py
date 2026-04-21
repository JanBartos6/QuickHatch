"""Regression tests for the sticky-block + pivot-detector helpers.

Motivated by a live run: pre-reboot guard blocked the reboot, agent
ran a few diagnostic commands, then pivoted to installing apps
instead of fixing the blockers. New helpers:

- `_extract_topic_words(failures)` — pulls block-relevant keywords
  from failure text so we can score subsequent commands.
- `_command_addresses_topics(cmd, topics)` — true iff the command
  mentions at least one blocked topic.
"""

from __future__ import annotations

import pytest

from quickhatch.gui.server import (
    _command_addresses_topics,
    _extract_topic_words,
    _is_safe_diagnostic_setup_command,
)


class TestExtractTopicWords:
    def test_encrypt_hook_failure_extracts_relevant_topics(self):
        failures = [
            "[luks-init] LUKS in use but mkinitcpio HOOKS lacks 'encrypt' — "
            "add 'encrypt' between 'block' and 'filesystems' in /etc/mkinitcpio.conf"
        ]
        topics = _extract_topic_words(failures)
        assert "encrypt" in topics
        assert "hooks" in topics
        assert "mkinitcpio" in topics
        assert "luks" in topics

    def test_locked_root_failure_extracts_recovery_topics(self):
        failures = [
            "[recovery] Root account is locked AND no sudo-capable user in 'wheel' or 'sudo'"
        ]
        topics = _extract_topic_words(failures)
        assert "root" in topics
        assert "wheel" in topics
        assert "sudo" in topics

    def test_stale_grub_cfg_extracts_grub_topic(self):
        failures = ["[grub-cfg] /etc/default/grub edited AFTER grub.cfg — run grub-mkconfig"]
        topics = _extract_topic_words(failures)
        assert "grub" in topics or "grub-mkconfig" in topics

    def test_empty_failures_returns_empty_set(self):
        assert _extract_topic_words([]) == set()

    def test_noise_words_not_captured(self):
        # Make sure a word that LOOKS technical but isn't in our vocab
        # (e.g. 'zfs') doesn't get captured.
        failures = ["[zfs] zfs-dkms missing"]
        topics = _extract_topic_words(failures)
        assert "zfs" not in topics  # not in _BLOCK_TOPIC_VOCAB


class TestCommandAddressesTopics:
    def test_empty_topics_means_no_active_block(self):
        # No topics = no block active = every command is "fine"
        assert _command_addresses_topics("pacman -S steam", set()) is True

    def test_sed_on_mkinitcpio_addresses_encrypt_block(self):
        topics = {"encrypt", "hooks", "mkinitcpio"}
        cmd = (
            "sed -i 's/^HOOKS=.*/HOOKS=(base udev encrypt filesystems)/' "
            "/etc/mkinitcpio.conf && mkinitcpio -P"
        )
        assert _command_addresses_topics(cmd, topics) is True

    def test_passwd_addresses_root_recovery_block(self):
        topics = {"root", "wheel", "sudo", "passwd"}
        assert _command_addresses_topics("passwd root", topics) is True

    def test_pacman_install_is_a_pivot_when_block_is_luks(self):
        # Agent running `pacman -S steam` while the block is about
        # LUKS/encrypt is a pivot.
        topics = {"encrypt", "hooks", "mkinitcpio", "luks", "cryptdevice"}
        cmd = "arch-chroot /mnt pacman -S --noconfirm steam brave-bin discord"
        assert _command_addresses_topics(cmd, topics) is False

    def test_yay_clone_is_a_pivot_when_block_is_recovery(self):
        topics = {"root", "wheel", "sudo"}
        cmd = "git clone https://aur.archlinux.org/yay-bin.git"
        assert _command_addresses_topics(cmd, topics) is False

    def test_sudo_wrapper_does_not_falsely_satisfy_recovery_block(self):
        topics = {"root", "wheel", "sudo", "sudoers", "passwd"}
        cmd = "sudo pacman -S --noconfirm steam"
        assert _command_addresses_topics(cmd, topics) is False

    def test_chroot_grub_mkconfig_addresses_grub_block(self):
        topics = {"grub", "grub-mkconfig", "cmdline"}
        cmd = "arch-chroot /mnt grub-mkconfig -o /boot/grub/grub.cfg"
        assert _command_addresses_topics(cmd, topics) is True

    def test_case_insensitive_match(self):
        topics = {"encrypt"}
        assert _command_addresses_topics("ENCRYPT /dev/sda2", topics) is True

    def test_substring_does_not_match(self):
        # 'encrypted' contains 'encrypt' but shouldn't word-boundary match.
        topics = {"encrypt"}
        assert _command_addresses_topics("echo encrypted-volume", topics) is False


class TestSafeDiagnosticSetupCommand:
    def test_ls_boot_is_allowed_as_read_only_diagnostic(self):
        assert _is_safe_diagnostic_setup_command("ls -lh /mnt/boot") is True

    def test_pacman_search_is_allowed_as_read_only_diagnostic(self):
        assert _is_safe_diagnostic_setup_command("pacman -Ss nvidia") is True

    def test_pacman_install_is_not_a_safe_diagnostic(self):
        assert _is_safe_diagnostic_setup_command("pacman -S --noconfirm steam") is False

    def test_git_clone_is_not_a_safe_diagnostic(self):
        assert _is_safe_diagnostic_setup_command(
            "git clone https://aur.archlinux.org/yay-bin.git /tmp/yay-bin"
        ) is False

    def test_echo_redirect_is_not_a_safe_diagnostic(self):
        assert _is_safe_diagnostic_setup_command(
            "echo '%wheel ALL=(ALL:ALL) ALL' > /etc/sudoers.d/10-wheel"
        ) is False


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
