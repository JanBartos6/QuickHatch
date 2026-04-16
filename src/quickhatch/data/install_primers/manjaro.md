# Manjaro Linux card

Family: **arch** — uses `families/arch-family.md` as the base procedure.
Arch-based with its own stability branches (stable / testing / unstable)
and curated kernel lineup. Calamares installer; also installable via
pacstrap with Manjaro's own mirrors.

## Per-distro values

| Key | Value |
|---|---|
| Live env | XFCE / KDE / GNOME edition, boots to DE as `manjaro` user |
| Branch | `stable` (default) / `testing` / `unstable` — picks how fresh packages are |
| Extra repo | Manjaro's own mirrors — **different from Arch mirrors**, not just an overlay |
| Pacstrap tarball | Use Manjaro's bootstrap image, NOT Arch's pacstrap tarball |
| Pacstrap extras | `manjaro-release manjaro-keyring manjaro-system pamac-cli base-devel` + chosen kernel (e.g. `linux61` / `linux66` / `linux-latest` — Manjaro ships multiple) + chosen DE flavor |
| Installer alternative | Calamares — the default user-facing installer |
| AUR helper | `pamac` (GUI + CLI) pre-installed; supports AUR toggle |
| sudo group | `wheel` |

## Repo setup (differs materially from Arch)

Manjaro does **not** use Arch's mirrors. Install from Manjaro's bootstrap
tarball:

```bash
# On the live env — download Manjaro's bootstrap image
wget https://mirror.manjaro.org/bootstrap/current/x86_64/manjaro-bootstrap.tar.gz
tar xzf manjaro-bootstrap.tar.gz -C /mnt

# Copy Manjaro's mirrorlist + pacman.conf into the target
cp /etc/pacman.d/mirrorlist /mnt/etc/pacman.d/mirrorlist
```

Alternatively — and this is what the family primer's pacstrap path does —
enable Manjaro's mirrors on the live env first:

```bash
echo 'Server = https://mirror.manjaro.org/mirrors.json' > /etc/pacman-mirrors.conf
pacman-mirrors --country all --fasttrack 5
pacman -Syy
```

Then pacstrap pulls from Manjaro's mirrors.

## Kernel choice

Manjaro ships multiple LTS + mainline kernels. The agent picks based on
hardware:

| Kernel package | Notes |
|---|---|
| `linux66` / `linux61` | LTS; safe defaults |
| `linux-latest` | Current mainline, track Arch |
| `linux-rt` | Realtime variant |
| `linux-zen` | Zen-patched (gaming-leaning) |
| `linux54` / `linux510` | Older LTS for specific hardware |

## DE flavor metapackages

- `manjaro-xfce-settings` + `xfce4 xfce4-goodies` — XFCE (default)
- `manjaro-kde-settings` + `plasma kde-applications` — KDE
- `manjaro-gnome-settings` + `gnome` — GNOME

## Distro-specific pitfalls

- **Never mix Manjaro + Arch repos.** Package version holding in Manjaro
  stable lags Arch by 1–2 weeks; mixing breaks the dependency solver.
- **AUR on Manjaro stable sometimes has churn.** AUR packages assume
  current Arch; if an AUR package pulls a package version Manjaro stable
  hasn't backfilled yet, build fails. Agent should prefer pamac + AUR
  over manual `makepkg`.
- **Branch switching is one-way-ish.** `pacman-mirrors --api --set-branch
  unstable` works, but going back to stable requires `pacman-mirrors` +
  a full sync, and some already-upgraded packages may be newer than
  stable. Don't flip branches mid-setup.
- **SSL cert expiry bug history.** Manjaro has had a few incidents where
  TLS cert rollover was late and pacman broke. If `pacman -Syy` fails
  with cert errors, agent can `pacman -S ca-certificates-utils` from the
  live env and update the target.
