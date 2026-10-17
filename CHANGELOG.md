# Changelog

All notable changes to QuickHatch will be documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- Released QuickHatch's own code and documentation under the MIT License,
  allowing commercial use, modification, redistribution, and sublicensing.
  Updated licensing documentation and package metadata while retaining existing
  contribution ownership terms and third-party licensing requirements.

## [0.1.0] - 2026-06-12

First public alpha release.

### Added

- Added community recipe gallery with about 100 curated Linux desktop
  configurations.
- Added host-side post-install data transfer for the "keep this PC on" path:
  selected local files or folders are copied over SSH into
  `~/QuickHatch-Imported-Windows`, while external-drive and cloud methods stay
  guided/manual.
- Fresh installs now use `password` as the temporary first-login password,
  remove the QuickHatch NOPASSWD sudo drop-in at the end of setup, and mark the
  installed user account to require a real password change on first login.
- Added a strict Linux desktop GUI contract test suite for promised theme,
  icon, cursor, wallpaper, and desktop-effect elements. Strict desktop runs now
  require each promised element to have a concrete expected value or an explicit
  manual-follow-up reason before success can be claimed.
- Documented contribution and fork licensing terms, made commercial-use contact
  language visible in the README, added sponsorship links, and noted the June 4,
  2026 real-PC Arch install driven by Gemini 3.1 Flash Lite.
- Added commercial licensing contact email, stronger source-available license
  terms, contribution assignment/licensing language, CODEOWNERS, and governance
  notes for maintainer-controlled merges to `main`.
- Added secret-pattern hygiene coverage so release sources fail tests if
  provider-token-shaped strings are introduced into tracked release files.
- Gallery Phase 1.5: loosened the Reddit filter so r/unixporn and DE-specific
  subs contribute entries. The previous "median + 1 stdev" upvote gate was
  replaced with per-subreddit absolute floors (500 for r/unixporn, 30-100 for
  DE subs) combined with a top-40%-of-bucket rule. Reddit entries with no
  detectable OSI license are admitted with `license_status: "unknown"` and a
  "No license" chip in the gallery; apply logic skips them so QuickHatch does
  not copy unlicensed work.

### Highlights

- Two modes: "Migrate from Windows" for fresh Linux installs on a target
  machine, and "Customize Linux" for tuning an existing install.
- Agentic SSH setup loop: the LLM picks one command, sees exit code plus
  stdout/stderr, and decides the next step. It adapts to errors such as missing
  packages, low disk space, and failed verifications.
- Live web and image search: the analysis phase can fetch DuckDuckGo results so
  the model gets current context on driver support, app alternatives, and
  desktop environment screenshots.
- End-to-end verified on Arch Linux on a real PC on June 4, 2026, driven by
  Gemini 3.1 Flash Lite.
- Web GUI with stepper navigation, agent feed, technical log, apps step with
  search and compatibility badges, error states, and toast notifications.

### Wizard

- Welcome -> AI setup -> Preferences -> Scan -> Apps & Data -> Notes ->
  Analysis -> Bootable USB -> SSH key -> Export -> Remote setup.
- Scans Windows hardware, installed apps, settings, file summary, fonts,
  browser profiles, and disk layout.
- Apps step shows scanned apps grouped by category, searchable, with native,
  alternative, or unknown compatibility badges.
- Apps & Data includes a structured data-transfer choice.
- Generates a sectioned migration plan covering distro, apps, drivers, desktop,
  migration steps, and quick-reference summary.
- Re-runs analysis when the user gives feedback to the agent.

### AI providers

- Claude API.
- OpenAI API.
- LM Studio, with auto-detection of loaded models via
  `localhost:1234/v1/models`.
- Ollama via `localhost:11434`.
- Codex CLI launched in a separate terminal.

### Remote setup

- SSH-based agent loop with Stop button.
- Test-connection button to verify SSH before launch.
- `host:port` format supported for the IP input.
- Setup agent reads the analysis suggestions as the blueprint instead of
  re-planning from scratch.
- Final audit and support-bundle paths redact secret-bearing values before
  exposing run artifacts.

### Repo and tooling

- 2663 pytest tests covering parsers, BridgeState, app mapping, prompt builders,
  search execution, setup orchestration, desktop GUI contracts, support bundle
  redaction, release smoke checks, and HTTP endpoints.
- GitHub Actions CI runs Ruff lint, Ruff format check, and pytest on normal
  pushes and pull requests.
- Manual "Full test matrix" workflow covers supported OS/Python combinations.
- GitHub Actions release workflow builds artifacts and creates a GitHub Release
  on `v*.*.*` tags, with PyPI trusted publishing configured in the workflow.
- `.pre-commit-config.yaml` for Ruff and standard hygiene hooks.
- `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, issue templates,
  PR template, `GOVERNANCE.md`, `.github/CODEOWNERS`, and funding metadata.
- Docs: `docs/ARCHITECTURE.md`, `docs/testing-in-vm.md`,
  `docs/model-recommendations.md`, and `docs/troubleshooting.md`.
- Source-available personal-use license with commercial use by separate written
  arrangement.

### Known limitations

- Data transfer is method-dependent. If the Windows/host PC stays online during
  remote setup, QuickHatch can copy selected scanned folders to the installed
  Linux user after setup. External-drive and cloud-sync methods remain
  guided/manual.
- No automated USB flashing. The "Bootable USB" step is a guided walkthrough;
  use Rufus, Ventoy, or balenaEtcher yourself.
- GPU passthrough is not handled in VirtualBox testing.
- Wayland plus VirtualBox mouse integration can be flaky without Guest
  Additions.
- The setup agent requires SSH key authentication. Password-only SSH is not
  supported.
- Arch is the most tested distro. Fedora and Ubuntu should work; other distros
  are at your own risk.
- Current modes are Windows-to-fresh-Linux migration and existing-Linux
  customization. Distro-to-distro switching is not implemented.
