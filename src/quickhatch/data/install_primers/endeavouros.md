# EndeavourOS install primer

Arch-based distro with Calamares installer + friendlier defaults. Ships
your choice of DE (KDE, XFCE, i3, BSPWM, Sway, Hyprland, Gnome, etc.).

## Live environment

- Boots to XFCE-ish live env (historical) or the user's chosen DE.
- Installer: Calamares GUI, or `eos-install` CLI.

## Approach — pacstrap (recommended, same as Arch)

Since EndeavourOS is Arch with extra repos, pacstrap is cleanest:

```bash
# Add EndeavourOS repo to pacman.conf on live env (for signing keys etc.)
cat >> /etc/pacman.conf <<'EOF'
[endeavouros]
SigLevel = Required DatabaseOptional
Include = /etc/pacman.d/endeavouros-mirrorlist
EOF

# Partition, mkfs, mount (same as Arch primer)

pacstrap -K /mnt base linux linux-firmware sudo networkmanager openssh \
  grub efibootmgr sof-firmware intel-ucode \
  endeavouros-mirrorlist endeavouros-keyring \
  eos-bash-shared eos-update-notifier \
  eos-welcome

genfstab -U /mnt >> /mnt/etc/fstab

# chroot setup — same as Arch primer plus enable EOS repo inside target
arch-chroot /mnt /bin/bash <<'EOF'
# Append EOS repo config inside target
cat >> /etc/pacman.conf <<'REPO'
[endeavouros]
SigLevel = Required DatabaseOptional
Include = /etc/pacman.d/endeavouros-mirrorlist
REPO

# ... standard Arch post-install (timezone, locale, user, ssh key, grub)

# Optionally install a DE:
pacman -Sy --noconfirm plasma kde-applications   # or hyprland
EOF
```

## Alternative — Calamares in unattended mode

EndeavourOS's Calamares supports `/etc/calamares/` config profiles. Running
it in fully automated mode is finicky; pacstrap is more reliable.

## Key packages that make it EndeavourOS vs plain Arch

- `eos-bash-shared` — shared scripts, custom fastfetch output, etc.
- `eos-update-notifier` — desktop notification for pending updates.
- `eos-welcome` — the welcome wizard with post-install tasks.
- `endeavouros-mirrorlist` + `endeavouros-keyring` — the EOS repo infra.

## Common pitfalls

- **AUR enabled by default on live but not installed** — the target
  install doesn't ship `yay`; agent can add it: `pacman -Sy yay` via the
  EOS repo (or build from AUR). Alternative: `paru`.
- **Systemd-boot vs GRUB** — EndeavourOS defaults to GRUB but supports
  systemd-boot; pick via Calamares or manual `bootctl install`.
- **DE choice** — live ISO is DE-agnostic; user picks at install. Agent
  reads `prefs.desktop_look` / gallery pick to decide which `plasma` /
  `gnome` / `hyprland` metapackage to install.
- **Online vs offline install** — online install fetches latest packages
  at install time (recommended). Offline is faster but stale.
