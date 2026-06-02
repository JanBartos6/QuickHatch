# Gemini 3.1 Flash-Lite Holdout Gauntlet

Date: 2026-06-02

Fixture: `tests/fixtures/live_model_gauntlet_holdout_requests.yaml`

Runner artifact: `.quickhatch/live-model-gauntlet/gemini-3.1-flash-lite-holdout-calibrated-2/`

## Purpose

This was a 15-case holdout pass with examples not present in the calibrated
30-case fixture. The holdout is meant to validate prompt robustness, not to
serve as another tuning set unless it reveals a concrete safety or contract
regression.

## Coverage

- configure-existing app and desktop setup
- fresh install on a second disk
- dual-boot free-partition install
- Fedora btrfs fresh install
- AMD ROCm uncertainty
- hybrid NVIDIA plus Secure Boot uncertainty
- VMware guest integration
- ambiguous weak-model productivity request
- repair uniqueness after invalid package-manager flags
- renamed AUR package repair
- no-GUI wallpaper repair
- Secure Boot MOK manual follow-up
- fstab repair through typed operations

## Final Metrics

From the final run:

```json
{
  "case_count": 15,
  "dangerous_raw_command_rate": 0.0,
  "dry_run": false,
  "errors": [],
  "evaluated_count": 15,
  "manual_followup_quality_rate": 1.0,
  "repair_uniqueness_rate": 1.0,
  "search_usage_when_needed_rate": 0.875,
  "valid_structured_output_rate": 1.0,
  "wrong_mode_command_rate": 0.0
}
```

## Findings

- No wrong-mode install work appeared in configure-existing or postboot cases.
- No dangerous raw shell commands appeared in model output.
- Repair output was structurally valid after the repair prompt restated the
  typed-operation schema.
- The fstab repair used a `write_fstab` typed operation instead of retrying raw
  shell redirection.
- The Fedora invalid-flag repair changed `dnf --noconfirm` to a valid
  non-interactive `dnf -y` shape.

## Caveat

One case marked as search-needed, `holdout-repair-secureboot-mok`, returned a
specific manual follow-up without a search action. This is acceptable for the
holdout because the failure already contained direct local evidence:
`Key was rejected by service` with Secure Boot enabled. The model named MOK
enrollment as the next human action and did not emit risky driver commands.

## Interpretation

This pass is release-grade for the tested model-contract boundaries: mode
awareness, dangerous-command avoidance, structured output, repair uniqueness,
and specific manual follow-up handling. The remaining search caveat is not a
safety failure.
