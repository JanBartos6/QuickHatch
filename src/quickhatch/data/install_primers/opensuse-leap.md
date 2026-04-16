# openSUSE Leap install primer

Point-release variant of openSUSE, synchronized with SUSE Linux Enterprise
Server. 18-month release cycle. Same YaST + AutoYaST as Tumbleweed.

## Live environment

- Offline installer ISO: boots to YaST.
- Same TTY access + same tools as Tumbleweed.

## Approach

**Use the Tumbleweed primer** — AutoYaST XML format is identical.

Only changes:

1. Repo URL in the `--root /mnt addrepo` command:
   ```bash
   zypper --root /mnt addrepo \
     http://download.opensuse.org/distribution/leap/<VERSION>/repo/oss/ oss
   # <VERSION> = 15.5, 15.6 etc.
   ```

2. For AutoYaST, no change — the profile format is shared across all
   SUSE/openSUSE variants.

## When to pick Leap over Tumbleweed

- User wants a **stable** system (Leap) rather than rolling (Tumbleweed).
- User needs SLE compatibility (enterprise apps).
- User wants longer-term releases without reboots for major updates.

## When Tumbleweed wins

- User wants latest kernel + packages.
- User is doing rice/gaming/modern-DE work that benefits from fresh deps.
- User is a developer wanting current language runtimes.

## Common pitfalls

- Same as Tumbleweed. Plus:
- **Leap kernel lags behind** — if user has very new hardware, Leap's kernel
  may not support it. Agent should check `uname -r` against GPU/WiFi model
  and recommend Tumbleweed if Leap's kernel is too old.
