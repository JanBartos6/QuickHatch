# Security Policy

## Threat Model

QuickHatch is experimental tooling that runs AI-generated shell commands as root on a target Linux machine via SSH. That makes it a higher-risk project than most. This document exists so you know what we're worried about and how to report when something goes wrong.

### What we care about most

1. **Prompt injection** — the agent reads web search results, scanned system data, and user-provided text. A malicious payload in any of those could trick the model into running unintended commands.
2. **SSH key handling** — we generate ed25519 keys and bundle them in the export ZIP. Anyone with the ZIP can SSH into the target.
3. **Arbitrary command execution** — by design, the setup agent runs shell commands as root on the remote machine.
4. **API key exfiltration** — API keys are stored in `.quickhatch-checkpoint.json` locally, and in a separate `.api_key` file inside export bundles. Both are gitignored; neither is transmitted anywhere except to the configured AI provider.

### What we explicitly do NOT protect against

- A malicious AI provider. If you point QuickHatch at an endpoint you don't trust, the agent will run whatever shell commands that endpoint returns.
- A malicious target machine. If someone else's SSH server is at the IP you enter, you've given them your profile data.
- A malicious migration bundle. If you receive a bundle ZIP from someone else, it can contain any SSH public key the sender chose.

## Supported Versions

QuickHatch is pre-1.0 and there's no security backport policy yet. The `main` branch is the only supported version. If you find an issue, we'll fix it in the next release.

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security problems.**

Use GitHub's **Report a vulnerability** / private security advisory flow if it is
enabled for the repository. If private vulnerability reporting is unavailable,
contact appdeveloper.honza@gmail.com; do not include exploit details, secrets,
SSH keys, or API keys in a public issue.

Include:

1. A description of the issue
2. Steps to reproduce (a minimal profile JSON + the model response that caused the behavior helps enormously)
3. What you think the impact is
4. Optionally, a suggested fix

We'll acknowledge within 72 hours. Valid reports will be credited in the release notes unless you prefer to stay anonymous.

## What counts as a vulnerability

Examples of things we'd treat as security issues:

- A scanner reading files it shouldn't (e.g., leaking contents outside the agreed scope)
- A path that lets injected web content rewrite the migration plan without visible indication
- SSH private keys ending up in the export ZIP (should only be the public key)
- API keys logged in plain text somewhere other than the checkpoint/bundle
- The agent accepting commands to run that shouldn't be permitted (e.g., a hypothetical "execute this on the host" escape)

Examples of things that are **not** vulnerabilities:

- The agent running a destructive command that was in the user's approved plan — that's the design
- A user's own AI provider giving a wrong answer
- Running out of disk space, timeouts, or model failures mid-migration
- The wizard asking for preferences you don't want to share (you can skip any step)

## Hardening recommendations for users

If you're using QuickHatch in anything like a real scenario:

- Always test against a VM before a real target
- Use a dedicated SSH key for the migration, revoke it when you're done
- Review the migration plan before clicking Launch on the Remote Setup step
- Keep backups; the agent can and will destroy data if the plan says so
- Pick an AI model you trust — see `docs/model-recommendations.md`
