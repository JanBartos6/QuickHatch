# EndeavourOS card

Family: **arch** — uses `families/arch-family.md` as the base procedure.
Arch-based, Calamares installer, friendlier defaults, pre-bundled `yay`.

## Per-distro values

| Key | Value |
|---|---|
| Live env | XFCE-ish live env; drop to TTY (Ctrl+Alt+F3) or terminal |
| Extra repo | `[endeavouros]` with `endeavouros-mirrorlist` |
| Pacstrap extras | `endeavouros-mirrorlist endeavouros-keyring eos-bash-shared eos-update-notifier eos-welcome` |
| Installer alternative | Calamares GUI; automation is finicky, prefer pacstrap path from family primer |
| AUR helper | `yay` comes preinstalled in the target image (after install) |
| sudo group | `wheel` |

## Repo setup (add BEFORE pacstrap)

```bash
cat >> /etc/pacman.conf <<'EOF'
[endeavouros]
SigLevel = Required DatabaseOptional
Include = /etc/pacman.d/endeavouros-mirrorlist
EOF
```

And again inside the chroot (append to `/mnt/etc/pacman.conf`) so the
installed system keeps the repo.

## Distro-specific pitfalls

- **Online vs offline install** — Calamares online fetches latest packages
  at install time (recommended). Offline ships a static snapshot. If using
  Calamares, pick online; if using pacstrap, it's always online.
- **DE choice** — live ISO is DE-agnostic; user picks at install time.
  Agent reads `prefs.desktop_look` / gallery pick to decide which DE
  metapackage to install after the base is up.
- **`yay` vs `paru`** — EOS ships `yay`. Users preferring `paru` can
  install it from AUR.
