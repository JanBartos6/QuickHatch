# QuickHatch Engineering Instructions

## Generalization Rule

QuickHatch harness fixes must generalize across the certified distro families unless a narrow exception is explicitly documented in the code and tests.

Certified families:
- Arch / pacman
- Ubuntu / apt
- Debian / apt
- Fedora / dnf

When fixing setup, package, driver, desktop, or audit behavior:
- Do not patch only the one package name, icon theme, app, or command from the latest failed run.
- Prefer package-manager, desktop-environment, driver-vendor, or setup-stage abstractions.
- Add at least one regression test for the observed failure.
- Add cross-family coverage when the behavior is package-manager or distro-family dependent.
- Add or extend `quickhatch debug harness-health` when the issue could waste a real install run.
- Keep hard blocks limited to safety/recovery boundaries: disk wipe ambiguity, bootability, SSH continuity, recovery user/sudo, wrong target root, or destructive commands outside the selected target.
- Package/app/theme resolution problems should normally be advisory, repairable, skipped as already satisfied, or explicit manual follow-up.

If a fix is intentionally family-specific, explain why in the function/test name or nearby comment.
