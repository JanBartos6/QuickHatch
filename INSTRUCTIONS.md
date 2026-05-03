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
- Keep hard blocks limited to safety/recovery boundaries: disk wipe ambiguity, bootability, SSH continuity, recovery user/sudo, package database integrity, wrong target root, or destructive commands outside the selected target.
- Package/app/theme resolution problems should normally be advisory, repairable, skipped as already satisfied, or explicit manual follow-up.

If a fix is intentionally family-specific, explain why in the function/test name or nearby comment.

## Harness Contract

The harness exists to protect the user and verify outcomes. It should not try to outguess the model on recoverable Linux details.

Hard blocks are allowed only for:
- Disk wipe, partitioning, or formatting ambiguity.
- Reboot before bootability checks pass.
- SSH continuity or reconnectability risk.
- Missing recovery path: no root password, no sudo-capable user, or no known noninteractive sudo path.
- Package database integrity risk, such as broad `pkill`/`killall` against package-manager processes that can leave dpkg/rpm/pacman state half-written.
- Destructive commands outside the selected target disk/root.
- VPN/firewall service activation that could cut off the remote session without explicit user consent.

Everything else should default to one of:
- `advisory`: warn the model and let it continue.
- `shadow`: record that the harness would have objected, but do not stop a safe run.
- `repairable`: let the command fail from real output, then let planning/repair handle it.
- `already_satisfied`: skip the mutation because refreshed facts prove the target state is already correct.
- `manual_followup`: surface a clear incomplete item to the user without pretending success.

Package names, package source choice, AUR/helper availability, theme names, app availability, display theming, and driver package variants are not safety boundaries by themselves. Do not hard-block them unless they directly threaten boot, disk safety, SSH continuity, or recovery.

## Resume And Verification

Before mutating an installed system on resume, refresh facts and run the stage audit first. If the audit passes, skip that stage and do not re-run package, driver, desktop, or app commands.

Never claim `done` from model text alone. Completion requires final audit to verify:
- The installed OS booted and SSH reconnects as the installed user.
- Sudo is noninteractive, or the known-password path is explicit and working.
- Bootloader, fstab, kernel/initramfs, and recovery checks are valid.
- Required driver state is `healthy`, `needs_reboot`, `not_applicable`, or an explicit manual follow-up category.
- Required desktop state is either verified live or honestly marked persisted/pending login/headless.
- Required apps are installed, explicitly skipped, or manual follow-up with a reason.

If final audit fails, fix the verifier or the orchestration generally. Do not add a one-off bypass for the latest failed app, theme, driver, or machine.

## Required Proof For Harness Changes

Every harness change that affects execution flow must include at least one fast regression path:
- Unit test for the exact observed failure category.
- Cross-family test when the behavior touches package managers, installers, bootloaders, services, drivers, or desktop environments.
- `quickhatch debug harness-health` coverage when the bug could waste a real install run.

For the four certified families, prefer tests that prove the invariant for:
- Arch / pacman
- Ubuntu / apt
- Debian / apt
- Fedora / dnf

Full VM installs are certification gates, not the primary debugging loop. First reproduce and fix failures with replay, unit tests, health checks, or mocked family adapters; then validate with VMs.
