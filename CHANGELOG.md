# Changelog

All notable changes to QuickHatch will be documented here. Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Full agentic SSH setup loop: LLM sees each command result and decides the next step, one action at a time
- Pre-fetched DuckDuckGo web + image searches for up-to-date analysis content (driver compat, app alternatives, DE screenshots)
- Direct image injection into the desktop suggestion (HEAD-validated URLs, skipped hotlink-blocked hosts)
- Per-section LLM calls for the analysis phase (distro, apps, drivers, desktop, plan, summary)
- Multi-JSON parser (`_extract_json_objects`) to handle models that emit multiple action objects per response
- "Customize Linux" mode for AI-driven configuration of an existing Linux system
- LM Studio model auto-detection (queries `localhost:1234/v1/models`)
- User feedback re-run: chat replies to the agent trigger a full regeneration with feedback baked into every section prompt
- MIT License
- `docs/ARCHITECTURE.md`, `docs/testing-in-vm.md`, `docs/model-recommendations.md`, `docs/troubleshooting.md`
- `CONTRIBUTING.md` with dev setup, code style, and starter tasks
- Test suite for the JSON parsers (`tests/test_parsers.py`, 17 tests)
- Demo screenshot of KDE Plasma running after a successful agentic migration

### Changed
- Plans are now prescriptive about end-state (activation commands, systemd service enablement, user account creation), not just package installs
- Markdown renderer in the GUI now supports tables, images with fallback, proper heading hierarchy, inline code, and escape-aware text processing
- AI provider list: Claude Code option removed (subscription discontinued), Claude API promoted to "recommended", OpenAI API split into its own choice, LM Studio gets a dropdown of loaded models
- USB step honestly labeled as a guided walkthrough rather than automated flashing

### Removed
- "Switching distros" welcome option (was a dead-end placeholder that did nothing different from fresh_install)
- Claude Code CLI integration (Anthropic discontinued the standalone subscription)

### Fixed
- Scan step now refreshes the client-side profile so subsequent steps see the scan results
- Agent no longer regenerates the plan from scratch during setup — it reads the plan from `bridge.suggestions`
- JSON parser handles literal newlines inside string values, markdown fences anywhere in the response, nested braces inside strings, and models that emit multiple objects in one response
- SSH exec supports custom ports via `host:port` format in the Linux IP field

### Known limitations
- No automated data transfer (user moves their files themselves)
- No automated USB flashing (user uses Rufus/Ventoy/balenaEtcher)
- GPU passthrough not handled in VirtualBox testing
- Wayland + VirtualBox mouse integration flaky without Guest Additions

## [0.1.0] - initial private release

- Wizard shell (terminal + web GUI)
- Windows system scanning (apps, hardware, settings, files)
- SSH key generation and export bundle
- Initial analysis agent and bridge HTTP API
