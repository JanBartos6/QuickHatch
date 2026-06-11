# Changelog

All notable changes to QuickHatch will be documented here. Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

- Added community recipe gallery with ~100 curated Linux desktop configurations.
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
- Documented the contribution and fork licensing terms, made commercial-use
  contact language visible in the README, added sponsorship links, and noted the
  June 4, 2026 real-PC Arch install with Gemini 3.1 Flash Lite.
- Added commercial licensing contact email, stronger source-available license
  terms, contribution assignment/licensing language, CODEOWNERS, and governance
  notes for maintainer-controlled merges to `main`.
- Gallery Phase 1.5: loosened the Reddit filter so r/unixporn and DE-specific
  subs actually contribute entries. Replaces the pathological "median + 1 stdev"
  upvote gate with per-subreddit absolute floors (500 for r/unixporn, 30–100
  for DE subs) combined with a top-40%-of-bucket rule. Adds a DE fallback
  from subreddit name (r/hyprland → hyprland) and a title/body showcase-hint
  gate for general DE subs. License gate softened for Reddit: entries with no
  detectable OSI license are admitted with `license_status: "unknown"` and a
  "No license" chip in the gallery — Phase 2 apply logic will skip them so
  we never copy unlicensed work.

## [0.1.0] — 2026-04-15

First public release.

### Highlights

- **Two modes:** "Migrate from Windows" (fresh Linux install on a target machine) and "Customize Linux" (tune an existing install).
- **Agentic SSH setup loop:** the LLM picks one command, sees the exit code + stdout/stderr, and decides the next step. Adapts to errors (missing packages, low disk, etc.).
- **Live web + image search baked in:** the analysis phase pre-fetches DuckDuckGo results so the model gets current info on driver support, app alternatives, and desktop environment screenshots.
- **End-to-end verified on Arch Linux** with KDE Plasma + 800+ packages installed by the agent in a VirtualBox VM, driven by Gemma 4 26B via LM Studio.
- **Polished web GUI:** rigid design system, dark mode, real stepper navigation, agent feed split into current-action / timeline / technical log, Apps step with search + categories + compat badges, error states with retry, toast notifications.

### Wizard

- Welcome → AI setup → Preferences → Scan → Apps & Data → Notes → Analysis → Bootable USB → SSH key → Export → Remote setup
- Scans Windows hardware, installed apps (winget + registry + PowerShell), settings, file summary
- Apps step shows all scanned apps grouped by category, searchable, with native/alternative/unknown compat badges
- Apps & Data has a structured "how will you move your files" question
- Generates a 6-section Markdown migration plan (distro, apps, drivers, desktop, plan, summary) with embedded screenshots
- Re-runs from scratch when the user types feedback to the agent ("Actually I want Arch")

### AI providers

- Claude API (recommended)
- OpenAI API
- LM Studio (with auto-detection of loaded models via `localhost:1234/v1/models`)
- Ollama (local)
- Codex CLI (launched in a terminal)

### Remote setup

- SSH-based agent loop with Stop button
- Test-connection button to verify SSH before launching the agent
- `host:port` format supported for the IP input
- Setup agent reads the analysis suggestions and uses them as the blueprint instead of re-planning

### Repo + tooling

- 111 pytest tests covering parsers, BridgeState, app mapper, prompt builders, search execution, agentic loop, and HTTP endpoints
- GitHub Actions CI: ruff lint + format + pytest matrix (Linux/macOS/Windows × Python 3.10/3.11/3.12)
- GitHub Actions release workflow: PyPI trusted publishing + GitHub Release on tag
- `.pre-commit-config.yaml` for ruff + standard hygiene hooks
- `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, issue + PR templates
- Docs: `docs/ARCHITECTURE.md`, `docs/testing-in-vm.md`, `docs/model-recommendations.md`, `docs/troubleshooting.md`
- Personal-use project license with commercial use by arrangement

### Known limitations

- At 0.1.0 launch, file transfer was guided/manual: QuickHatch described how
  to move files, but did not copy them automatically.
- No automated USB flashing — guided walkthrough only (use Rufus, Ventoy, or balenaEtcher)
- GPU passthrough not handled in VirtualBox testing
- Wayland + VirtualBox mouse integration is flaky without Guest Additions
