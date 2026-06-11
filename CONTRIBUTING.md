# Contributing to QuickHatch

Thanks for your interest. QuickHatch is experimental and there are plenty of rough edges — contributions are welcome.

## Contribution ownership and license

By submitting a pull request, patch, issue attachment, or other contribution for
inclusion in the official QuickHatch project, you agree that the contribution is
assigned to Jan Bartos to the maximum extent permitted by law. To the extent
assignment is not effective or not permitted, you grant Jan Bartos an
irrevocable, perpetual, worldwide, royalty-free, transferable, sublicensable
license to use, reproduce, modify, distribute, commercialize, and relicense the
contribution as part of QuickHatch and related commercial offerings.

Only submit work you have the right to license this way. Do not include
third-party code, assets, prompts, images, or generated output unless the
license is compatible with QuickHatch's license and the attribution requirements
are clear in the PR.

Forks and modified versions must keep the QuickHatch license notice and remain
subject to the same non-commercial terms unless Jan Bartos grants a separate
written license. Commercial use is not granted by the public license; for
commercial use, contact Jan Bartos at appdeveloper.honza@gmail.com so you can
figure out appropriate terms.

## Dev setup

```bash
git clone https://github.com/JanBartos6/QuickHatch.git
cd QuickHatch
pip install -e ".[dev]"    # includes pytest + ruff
```

Python 3.10+ required. Most work is done on Windows since that's the primary user target, but the codebase runs on Linux/macOS too (useful for `--demo` runs).

## Running the wizard

**Terminal:**
```bash
python -m quickhatch
```

**Web GUI (recommended):**
```bash
python -m quickhatch --gui
```

**Demo mode** — loads a sample profile, skips the scan step:
```bash
python -m quickhatch --gui --demo
```

## Running tests

```bash
pytest
```

To add a test, drop a file under `tests/` named `test_*.py`. See `tests/test_parsers.py` for an example that covers the JSON extraction helpers.

## Linting

We use [ruff](https://github.com/astral-sh/ruff):

```bash
ruff check src/
ruff format src/ --check
```

CI will reject PRs that fail either.

## Code style

- **Python:** follow PEP 8, enforced by ruff. Keep line length to 100.
- **Type hints:** use them on public functions. Private helpers can skip.
- **Docstrings:** short triple-quoted strings on public functions and modules. No essays.
- **Imports:** group stdlib → third-party → local, separated by blank lines.
- **JavaScript (in `app.html`):** stay consistent with the existing inline style. No build step — keep it vanilla JS in a single file.

## Project layout

```
src/quickhatch/
├── cli.py              # entry point, argparse, main loop
├── config.py           # UserProfile + AIConfig dataclasses
├── console.py          # rich-based printing helpers
├── scanners/           # hardware, apps, settings, files
├── analysis/           # distro recommendation engine
├── wizard/             # terminal wizard step modules
├── bridge/             # BridgeState + HTTP API for agent comms
├── gui/
│   ├── app.html        # single-page web UI
│   └── server.py       # GUI HTTP server + setup agent loop
├── ai/                 # prompt generation
└── export/             # migration bundle writer

tests/                  # pytest tests
docs/                   # architecture, troubleshooting, etc.
```

## What needs work

Good starter tasks:

- **More parser tests** — edge cases in `_extract_json_objects` and `_parse_agent_response`
- **Better error recovery** in `_run_setup_agent` — right now a single SSH timeout can end the loop
- **Ollama + Anthropic testing** — the tool-use paths exist but have less verification than LM Studio
- **Multi-distro testing** — Fedora and Ubuntu need the same treatment Arch got
- **Architecture docs** — `docs/ARCHITECTURE.md` exists but could use diagrams

Check the [issues tab](https://github.com/JanBartos6/QuickHatch/issues) for anything tagged `good first issue`.

## Pull requests

- One topic per PR. Split refactors from features.
- Include a test if you're adding non-trivial logic.
- Update `CHANGELOG.md` under the `## [Unreleased]` heading.
- Don't update version numbers — maintainers handle releases.
- Jan Bartos is the sole maintainer for now and the only person trusted to merge
  into `main`. External contributors should work through pull requests.

## Commit messages

Short, imperative, under 72 chars for the subject line. Body optional but helpful for explaining *why*. Example:

```
Fix agent hang when ssh_exec returns non-UTF-8 output

The subprocess call was failing silently on Windows when pacman printed
box-drawing characters. Decoding with errors="replace" fixes it.
```

## License

By contributing, you agree to the contribution ownership and licensing terms
above and to the QuickHatch Source-Available Personal Use License in
[LICENSE](LICENSE).
