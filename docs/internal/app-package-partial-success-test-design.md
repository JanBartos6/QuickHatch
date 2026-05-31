# App/package partial success test oracle

This note defines the invariant oracle for future harness/orchestrator tests around app
package execution and partial success. It is design-only: tests should use fake facts,
fake executors, fake model responses, and fake runlogs. They must not use Docker, VMs,
real SSH, or real package managers.

## Scope

Exercise the app-stage behavior through `SetupOrchestrator` where practical:

- app plan rows: `AppPlanItem.status`, `required`, `reason`, and package/source fields
- app command groups: package extraction, prefiltering, command rewrite, verification, and group status
- repair loop outcomes: retry group, search actions, provider errors, question timeout, manual follow-up
- final verification: `verified_installed_apps`, `verified_alternative_apps`,
  `unverified_installed_apps`, and `app_verification_manual_followups`
- runlog/support evidence: app package precheck, repair actions, final app verification,
  manual follow-up entries, and final status

The oracle is effect-based. A package command may be wrong, stale, or unavailable without
becoming a dangerous setup failure. The test should fail only when the harness loses
progress, smears one app's failure across unrelated apps, hides unresolved work, or reports
an unearned success.

## Invariants

1. Successful app status is monotonic.
   Once an app becomes `INSTALLED` or `COMPLETED` and final verification has a matching
   signal, unrelated later app failure must not regress it to `PENDING`, `FAILED`, or
   `MANUAL_FOLLOWUP`.

2. Failure isolation.
   A missing/unavailable package must affect only mapped app rows. Isolation may use package
   aliases, explicit `app_names`, command text, and group package metadata, but it must not
   mark every app in a mixed command group as unresolved.

3. Optional app failure is non-fatal.
   Optional app, theme, icon, font, or cosmetic failure should become manual follow-up or a
   warning. It must not set final status `blocked` unless the failure also proves a dangerous
   setup invariant failed.

4. Required app failure is honest.
   A required app left unresolved, failed, blocked, or manual-followup without verification
   must prevent final status `success`.

5. Provider repair failure preserves progress.
   Model/provider 429s, timeouts, invalid JSON, or repair search errors may stop repair for
   the affected app/group, but they must not erase app rows already installed or verified.

6. Strategy changes are not repeated loops.
   A transition such as repo package -> AUR/community package -> Flatpak/Snap candidate is
   a new strategy attempt. Retrying the same command, package set, and failure signature is
   a loop and should become manual follow-up for non-install stages once the loop guard trips.

7. Alternatives are honest.
   Installing an alternative must populate `verified_alternative_apps` and produce warning
   status/copy. It must not add the original app to `verified_installed_apps`.

8. Final status honesty.
   `success` is illegal if unresolved app manual follow-up remains in plan rows, command
   groups, final verification details, or final audit repair details.

9. Manual follow-up specificity.
   Manual follow-up must name the affected app(s) and include a useful reason. Generic group
   follow-up is acceptable only when no app row can be mapped.

10. Facts refresh does not erase decisions.
    Refreshed facts update target reality and may verify previously unresolved work, but a
    refresh must not drop unresolved/manual-followup state unless a later verification signal
    proves the app is installed or intentionally handled as an alternative.

## Proposed Scenario Fixture Schema

Use a compact YAML or inline parametrized structure. Keep each scenario close to one
invariant and avoid a giant corpus.

```yaml
cases:
  - id: partial-vlc-success-spotify-provider-429
    invariant_ids: [1, 2, 5, 8, 9, 10]
    facts:
      family: debian
      package_manager: apt
      is_live: false
    apps:
      - name: VLC
        package_name: vlc
        source: repo
        required: true
        initial_status: pending
      - name: Spotify
        package_name: spotify-client
        source: repo
        required: true
        initial_status: pending
    command_groups:
      - id: apps-main
        stage: apps
        title: Install media apps
        commands:
          - sudo apt install -y vlc spotify-client
        packages: [vlc, spotify-client]
    package_db_snapshots:
      - before_group: []
      - after_failed_group: [vlc]
      - final_audit: [vlc]
    executor_events:
      - match: "install -y vlc spotify-client"
        rc: 1
        stdout: ""
        stderr: "Unable to locate package spotify-client"
    model_events:
      - action: provider_error
        error: "429 rate limit"
    expected:
      app_status:
        VLC: installed
        Spotify: manual_followup
      final_status: manual_followup_required
      verified_installed_apps: [VLC]
      unverified_installed_apps: []
      manual_followup_contains:
        app: Spotify
        reason_contains: "429"
      forbidden:
        app_status:
          VLC: manual_followup
        final_status: success
```

Recommended fields:

- `facts`: distro/package-manager/live/user inputs for `SetupFacts`
- `apps`: initial app rows plus required/optional flags
- `command_groups`: app-stage command groups
- `package_db_snapshots`: ordered native package DB states returned by fake precheck/final verification
- `executor_events`: command matchers and deterministic `(rc, stdout, stderr)` results
- `model_events`: scripted repair actions, searches, provider errors, invalid JSON, or question timeout
- `fact_sequence`: optional refreshed facts sequence
- `expected`: plan row statuses, final verification details, runlog evidence, final status, and forbidden states

## Proposed Fake Harness Design

- `FakePackageDb`: returns package-query snapshots by phase (`prefilter`, `partial_group`,
  `final_audit`) and records which package manager query was used.
- `FakeSSH`: pattern-matches command strings and returns configured results. It should
  recognize package DB queries, Flatpak/Snap/binary/desktop probes, and app command failures.
  It must never execute shell.
- `FakeFactsProvider`: returns a sequence of `SetupFacts` and records refresh kwargs. Use it
  to prove facts refresh changes reality without clearing decisions.
- `FakeRepairModel`: returns scripted repair actions or raises provider-style errors. Keep
  separate events for search action, retry group, invalid JSON, timeout, and manual follow-up.
- `MemoryRunLog`: capture app precheck, command, repair, final verification, and final audit
  events for assertions.
- `assert_app_invariants(orchestrator, expected)`: one helper that checks monotonic status,
  isolation, final verification details, final status, manual-followup specificity, and
  absence of forbidden transitions.

Prefer testing through `_run_stage_groups(SetupStage.APPS)` plus
`_compute_final_audit_result()` for end-to-end orchestration invariants. Use direct helper
tests only for parser-specific cases such as missing-package extraction or alias matching.

## Existing Coverage

- `tests/test_setup_facts_refresh.py`
  - app group refresh feeds final app verification
  - partial app success survives later repair manual follow-up
  - app/provider manual-followup decisions survive facts refresh

- `tests/test_setup_orchestrator.py`
  - AUR PGP key failures isolate affected packages and retry remaining packages
  - missing repo target repair waits for search/repair before manual follow-up
  - stale package hint can resolve through repo/community search before follow-up
  - optional slow apps become manual follow-up before app audit
  - final package verification accepts native package, Flatpak, Snap, binary, and desktop-entry signals
  - final verification rejects weak/generic app aliases
  - explicit alternatives are reported separately from verified installed apps
  - direct helper coverage preserves partially installed packages when marking group follow-up
  - repair model unavailable, invalid JSON exhaustion, and repair question timeout become manual follow-up for app/desktop stages

- `tests/test_setup_final_audit.py`
  - optional app issues become manual follow-up instead of fatal block
  - required failed app changes final status to `manual_followup_required`
  - plan-level manual follow-up app rows prevent success
  - alternative installed becomes `success_with_warnings`
  - terminal final status matrix distinguishes success, warning, manual follow-up, unsupported safe, and blocked

- `tests/test_support_bundle.py`
  - support bundle exports final app verification signals, unverified apps, app verification manual follow-ups, and final repair manual follow-ups

## Missing Tests

- Full-stage monotonicity: one app verified/installed, a later unrelated app group fails, and
  the first app remains installed through final audit.
- Full-stage failure isolation for a mixed command group with three apps where only one package
  is missing after execution; assert unrelated apps remain pending/installed as appropriate.
- Optional non-slow app failure path through `_run_stage_groups(APPS)`, not only optional slow
  confirmation or desktop cosmetic verification.
- Provider 429/timeout during app repair after partial package DB success; assert installed
  rows and final verification evidence are preserved.
- App-stage strategy-change loop oracle: repo -> community/AUR -> Flatpak/Snap should not trip
  the identical-loop guard, but repeating the same app package command should.
- Manual-followup specificity when repair returns no `app_names` and the group title is generic;
  assert the harness maps only affected apps and records reasons with app names.
- Final status honesty through the full `_run_final_audit()` path when app manual follow-up is
  present only in stage state or final verification details.
- Support bundle integration for stage-level app manual follow-up entries that are later surfaced
  through final audit details, including app name and reason.
- Facts refresh plus verification reconciliation: a manual-followup app should clear only when a
  later final verification signal proves the same app or records an honest alternative.
