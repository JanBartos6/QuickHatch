# QuickHatch Agent State

This is a lane scoreboard for future agents. It is not a commit log. Evidence is
only recorded when it is known from the working context; otherwise the field says
`unknown`.

Last updated: 2026-05-29

## 1. Preflight / capabilities

- Objective: Keep dangerous setup effects capability-gated while allowing
  recoverable command mistakes to remain advisory or repairable.
- Measurable pass target: Capability/preflight tests pass for destructive
  disk, boot, fstab, package database, network, SSH-risk, and shell-wrapper
  cases; harmless diagnostics, package searches, stale package hints, and app
  repair attempts do not hard-block unless they contain a dangerous effect.
- Latest known evidence: `quickhatch.cli debug release-smoke` passed on
  2026-05-28 and included setup capability, preflight, health, final audit,
  orchestrator, bridge, GUI endpoint, support bundle, and release smoke tests.
  Prior project context says full pytest and release-smoke passed around main
  `6dff155`.
- Open blockers: Complete current coverage for all shell wrappers, variables,
  heredocs, and command-substitution hiding patterns is unknown.
- Next action: Run or extend the calibration matrix for false positives and
  false negatives, then add regression tests for any concrete gap found.

## 2. Model reliability

- Objective: Make model-produced plans usable, repairable, and honest without
  letting malformed or overreaching output bypass setup guardrails.
- Measurable pass target: Deterministic model-shim runs can plan and execute
  safe configure-existing changes; invalid actions are rejected with clear
  status; repair loops do not turn planning prompts into bogus repair plans.
- Latest known evidence: A local OpenAI-compatible deterministic shim was used
  on 2026-05-28 for Fedora and Debian configure-existing runs. The first shim
  misclassified planning prompts as repair and produced blocked runs; the second
  shim produced safe configure-existing plans that completed with
  `manual_followup_required` and no blockers.
- Open blockers: Reliability with real hosted models, local LLMs, rate limits,
  malformed streaming output, and broader prompt variance is unknown.
- Next action: Add small deterministic model-shim scenarios for valid plan,
  invalid action, repair, and malformed response paths.

## 3. Configure-existing

- Objective: Exercise already-installed systems through the harness in
  configure-only mode for reversible desktop/app changes without arming
  destructive install intent.
- Measurable pass target: Fedora and Debian existing-install VMs can run
  configure-existing through the model path, apply reversible font/wallpaper/app
  changes, avoid destructive disk/boot/fstab/network/SSH changes, and end with
  an honest final status.
- Latest known evidence: Fedora run `56153377` and Debian run `628e910f`
  completed with `manual_followup_required`, no blockers, and
  `destructive_mode_armed=false`. SSH verification showed font/wallpaper marker
  files and a `nano` install/remove lifecycle marker; `nano` was absent after
  the run. The headless/minimal VM environment prevented full live desktop
  wallpaper verification.
- Open blockers: Live graphical-session verification for wallpapers, themes,
  fonts, and desktop settings is unknown.
- Next action: Add one graphical or better-instrumented configure-existing VM
  scenario, or explicitly document the headless verification boundary.

## 4. Full install endurance

- Objective: Keep full-disk VM installation certification repeatable across
  supported families, especially after setup/orchestrator changes.
- Measurable pass target: Deterministic full-disk certification passes cleanly
  for Arch, Ubuntu, Debian, and Fedora, including post-reboot audit, with no
  unexplained rerun-only success.
- Latest known evidence: Project context says deterministic full-disk VM
  certification passed for Arch, Ubuntu, Debian, and Fedora, and Arch AI fresh
  full-disk VM passed. Fedora and Debian installed VM targets were preserved and
  reused for configure-existing on 2026-05-28 after post-reboot audit passed.
  Release-smoke on 2026-05-28 passed dry-run certification checks for Arch,
  Ubuntu, Debian, and Fedora.
- Open blockers: Latest clean live full-disk rerun results for all four
  families after the most recent changes are unknown.
- Next action: Run live full-disk certification for Fedora and Debian first,
  then Arch and Ubuntu if time and host capacity allow.

## 5. Red-team safety

- Objective: Prove adversarial or accidental model output cannot perform
  destructive operations without capability, explicit consent, or exact later
  confirmation.
- Measurable pass target: Red-team commands for disk wiping, partition writes,
  cryptsetup destructive operations, fstab replacement, bootloader mutation,
  package database surgery, VPN/firewall activation, SSH/network shutdown,
  target-root deletion, and obfuscated shell forms are blocked without the
  required capability or consent.
- Latest known evidence: Project context says dangerous operations are
  capability-gated and final audit statuses/support bundles exist. Release-smoke
  on 2026-05-28 passed the setup safety test subset. Dedicated red-team
  adversarial-model evidence is unknown.
- Open blockers: Coverage for prompt-injected command wrappers and staged
  multi-command payloads is unknown.
- Next action: Build a focused red-team fixture that feeds unsafe command groups
  into preflight/orchestrator checks without requiring real SSH, model APIs, or
  VMs.

## 6. Codebase simplification

- Objective: Reduce complexity only where it makes the harness easier to audit
  while preserving installer architecture, safety semantics, and the single-file
  GUI constraint.
- Measurable pass target: Any simplification removes duplicated or confusing
  logic, has regression coverage, and does not broaden runtime behavior or
  frontend architecture.
- Latest known evidence: unknown.
- Open blockers: Current simplification candidates and risk ranking are
  unknown.
- Next action: Identify two or three high-leverage simplification candidates
  from setup orchestration, GUI state handling, or test helpers, then choose the
  lowest-risk one with measurable coverage.
