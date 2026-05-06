# QuickHatch Architecture

A walk through how the pieces fit together, so you can jump into the code without reverse-engineering it.

## The three-box mental model

```
┌────────────────────────────┐    ┌──────────────────────┐    ┌──────────────────────┐
│  HOST (Windows)            │    │  AI PROVIDER         │    │  TARGET (Linux)      │
│                            │    │                      │    │                      │
│  ┌──────────────────────┐  │    │  ┌────────────────┐  │    │  ┌────────────────┐  │
│  │ QuickHatch wizard    │  │    │  │  LM Studio     │  │    │  │                │  │
│  │  - scanners/         │◄─┼────┼──┤  or Anthropic  │  │    │  │  Arch / Fedora │  │
│  │  - wizard/ (TUI)     │  │    │  │  or OpenAI     │  │    │  │  / Ubuntu /    │  │
│  │  - gui/ (HTML + HTTP)│  │    │  │  or Ollama     │  │    │  │  whatever      │  │
│  └──────────────────────┘  │    │  └────────────────┘  │    │  │                │  │
│                            │    │                      │    │  │  SSH server    │  │
│  ┌──────────────────────┐  │    │                      │    │  │  (the only     │  │
│  │ Setup agent          │  │    │                      │    │  │   thing we     │  │
│  │  (agentic SSH loop)  │──┼────┼──────────────────────┼────┼──►  need there)   │  │
│  └──────────────────────┘  │    │                      │    │  │                │  │
│                            │    │                      │    │  └────────────────┘  │
│  ┌──────────────────────┐  │    │                      │    │                      │
│  │ Bridge (HTTP)        │  │    │                      │    │                      │
│  │  shared state for    │  │    │                      │    │                      │
│  │  UI ↔ agent comms    │  │    │                      │    │                      │
│  └──────────────────────┘  │    │                      │    │                      │
└────────────────────────────┘    └──────────────────────┘    └──────────────────────┘
```

The host is where everything runs: the wizard, the GUI server, the LLM client, and the setup agent. The AI provider is called over HTTP. The Linux target is only ever reached via SSH.

## Directory layout

```
src/quickhatch/
├── cli.py              # argparse + main loop, picks TUI vs GUI
├── config.py           # UserProfile + AIConfig dataclasses (checkpointed to JSON)
├── console.py          # rich-based printing helpers
│
├── scanners/           # read state FROM the host Windows system
│   ├── apps.py         # winget + registry + PowerShell
│   ├── hardware.py     # CPU, RAM, GPU, WiFi, Bluetooth, disks
│   ├── settings.py     # theme, default browser, locale
│   └── files.py        # files summary for the plan
│
├── analysis/
│   ├── distro_engine.py   # simple rule-based distro recommender (legacy)
│   └── app_mapper.py      # heuristic Windows→Linux app name mapping
│
├── wizard/             # terminal wizard step modules (TUI mode)
│   ├── welcome.py
│   ├── ai_setup.py
│   ├── questionnaire.py
│   ├── scan.py
│   ├── additional.py
│   ├── bridge.py       # starts the bridge server for the TUI
│   ├── usb.py          # guided walkthrough, no real flashing
│   ├── ssh.py          # ssh-keygen wrapper
│   └── export.py       # writes the migration bundle ZIP
│
├── bridge/
│   ├── server.py       # BridgeState + HTTP handlers (suggestions, questions, messages)
│   └── display.py      # rich-based live TUI display
│
├── gui/
│   ├── app.html        # single-page SPA: all UI state, step routing, polling
│   ├── server.py       # GUI HTTP server + the setup agent + tool-use loops
│   └── __init__.py
│
├── ai/
│   └── instruction_gen.py  # generates the migration instructions markdown
│
└── export/
    └── __init__.py     # (mostly empty, export logic lives in wizard/export.py)
```

## Two distinct modes

### Setup mode maturity

Remote setup has different maturity levels depending on how much disk state it can change:

| Mode | Status | Boundary |
|---|---|---|
| Configure existing Linux | Beta | Configure packages, drivers, desktop, services, and user state on an installed target. No partitioning, formatting, or distro reinstall. |
| Fresh install, full disk | Experimental | Full-disk live-ISO install on one explicitly confirmed target disk. Requires typed dangerous-operation capabilities before disk/boot/fstab changes. |
| Dual boot | Advanced / experimental | Use only pre-created free space or explicit mappings. Never shrink Windows or delete NTFS, BitLocker, recovery, or existing EFI partitions automatically. |
| Manual partition mapping | Dangerous / expert | Only execute exact user-confirmed partition operations; wrong input can destroy data. |

### Analysis phase (pre-install)

1. User runs `python -m quickhatch --gui`.
2. `gui/server.py` starts an HTTP server on a random port. The SPA at `app.html` is served at `/`.
3. User walks through steps: Welcome → AI Setup → Preferences → Scan → Apps/Data → Additional → **Bridge** (agent analysis).
4. At the bridge step the user clicks "Launch agent". This triggers `_run_agent_background` in `gui/server.py`:
   - **Pre-fetch** — `_prefetch_search_context()` runs DuckDuckGo text + image searches based on the user's hardware and preferences. No LLM involvement yet.
   - **Section-by-section LLM calls** — for each of (distro, apps, drivers, desktop, plan, summary) the server calls `_call_model_simple()` with a focused prompt that includes the pre-fetched search context.
   - Each section is posted to the bridge as a `Suggestion`. The SPA polls `/api/suggestions` and renders them as cards.
5. If the user chats to the agent ("Actually I want Arch, not Pop!_OS"), `/wizard/rerun-agent` clears old suggestions and re-runs with `user_feedback` injected into every section prompt.

Key design choices:
- **No tool-use during analysis.** We tried, Gemma 26B ignored the tools. Pre-fetching searches is more reliable.
- **One LLM call per section.** Smaller prompts, cleaner output, parses cleanly to separate cards in the UI.
- **Desktop images are injected directly**, not synthesized by the LLM. URLs are validated with a HEAD request before embedding.

### Setup phase (post-install)

1. User installs Linux on the target (manually, from USB).
2. User runs the quickstart.sh from the export bundle on the new Linux system. This enables `sshd` and prints the IP.
3. User enters IP + username in the QuickHatch Remote Setup step, clicks "Launch agent".
4. This triggers `_run_setup_agent` in `gui/server.py`:
   - **SSH connectivity test.**
   - **Gather remote system info** (one big SSH command: `/etc/os-release`, `df`, `free`, `lspci`, package manager detection, whoami).
   - **Build agentic loop system prompt** that includes: the migration plan from the analysis phase (pulled from `bridge.suggestions`), the remote system info, and the full user profile.
   - **Loop** — each iteration:
     1. Send conversation history to the LLM.
     2. Parse the response with `_extract_json_objects`. The model responds with one or more action objects: `{"action": "ssh_exec", "command": "...", "reason": "..."}`, `{"action": "status", "message": "..."}`, or `{"action": "done", "summary": "..."}`.
     3. Execute each `ssh_exec` action. Capture stdout/stderr/exit code.
     4. Feed consolidated feedback back to the model as the next user message.
   - **Stop conditions:** model returns `done`, or 100 iterations elapsed.

Key design choices:
- **One command at a time.** The model sees each result before deciding the next step. This is what gives it the ability to diagnose `xf86-video-vmware not found` and pivot to kernel modesetting.
- **Output truncation.** stdout capped at 2000 chars, stderr at 1000. Protects the model's context window.
- **Multi-JSON parsing.** Gemma frequently emits `status + ssh_exec` in the same response. `_extract_json_objects` handles that via brace-counting with proper string handling.

## The bridge

`bridge/server.py` provides shared state between two threads that don't know about each other:

- One side is the wizard UI (TUI or GUI).
- The other side is either the user's external AI tool (which HTTP-POSTs to `/api/*`) or our own agent running in a background thread.

`BridgeState` holds suggestions, messages, and pending questions. Everything is lock-protected. The GUI SPA polls every 2 seconds while on the bridge or setup step.

Endpoints:
- `GET /api/profile` — returns the full user profile JSON
- `GET /api/suggestions` — current list of suggestions with their content
- `GET /api/messages` — chat messages between user and agent
- `GET /api/pending-questions` — agent-asked questions waiting for user input
- `GET /api/status` — counts + flags for the polling UI
- `POST /api/suggest` — agent posts a new suggestion
- `POST /api/message` — agent or user posts a chat message
- `POST /api/ask` — agent asks a question, blocks until user answers
- `POST /api/done` — agent says it's finished

## Scanners

Windows-only by default. Scanner results go into the `UserProfile` dataclass and are written to `.quickhatch-checkpoint.json` after every step (so the wizard is resumable).

- **apps.py** uses three strategies: `winget list` (most structured), registry `HKLM\...\Uninstall\*` (most complete), and a PowerShell fallback.
- **hardware.py** uses `wmic`, `Get-WmiObject`, and `Get-NetAdapter`.
- **files.py** produces a files summary (counts, sizes, top extensions) without reading any actual file content — privacy first.

All scanners have Linux/macOS fallbacks for developing on non-Windows.

## Export bundle

`wizard/export.py` produces a ZIP with:

- `MIGRATION_INSTRUCTIONS.md` — the big Markdown brief for any AI agent
- `quickhatch-profile.json` and `.yaml` — the full profile (API key redacted)
- `.api_key` — the API key, separately, with owner-only permissions
- SSH public key (if one was generated)
- `quickstart.sh` — bash script to run on the new Linux machine to enable SSH and print the IP

## Things that are NOT here

- No USB flashing (guided instructions only)
- No Windows→Linux file copying (we never touch user data)
- No automated Linux install (user installs Linux themselves from USB)
- No package management abstraction (the LLM generates distro-specific commands)
- No GUI installer or desktop wrapper at the moment
