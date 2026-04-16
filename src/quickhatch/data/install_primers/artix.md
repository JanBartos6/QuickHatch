# Artix Linux card

Family: **arch** — uses `families/arch-family.md` as the base procedure.
**Arch without systemd.** Ships with a choice of init systems: OpenRC,
runit, s6, or dinit. Otherwise identical package set to Arch.

## Per-distro values

| Key | Value |
|---|---|
| Live env | Boots to shell or DE (per-edition ISO); login as `artix` or `root` |
| Init | Pick ONE: OpenRC (most common) / runit / s6 / dinit. Determines base package + service commands. |
| Extra repo | Artix's own: `[world] [galaxy] [lib32]` in `/etc/pacman.conf` — **different from Arch mirrors** |
| Arch overlay | Optional `[universe]`, `[extra]` from Arch (Artix-packaged, systemd-stripped) |
| Pacstrap base (OpenRC) | `base base-devel openrc-base elogind-openrc linux linux-firmware sudo openssh networkmanager networkmanager-openrc grub efibootmgr` |
| Pacstrap base (runit) | Swap `-openrc` suffixes for `-runit` (`runit base-runit networkmanager-runit` etc.) |
| Pacstrap base (s6) | Swap for `-s6` / `s6-base` / `s66-config` |
| Pacstrap base (dinit) | Swap for `-dinit` / `dinit-base` |
| Installer alternative | Calamares (pick init at install time); prefer pacstrap path |
| AUR helper | Not pre-installed; `yay` / `paru` buildable from AUR |
| sudo group | `wheel` |

## Init-specific service commands

The family primer's `systemctl enable NetworkManager sshd` line **doesn't
work** on Artix. Swap based on init:

| Init | Enable service |
|---|---|
| OpenRC | `rc-update add NetworkManager default` then `rc-service NetworkManager start` |
| runit | `ln -s /etc/runit/sv/NetworkManager /etc/runit/runsvdir/default/` |
| s6 | `s6-rc-bundle-update add default NetworkManager` (or edit `/etc/s6/adminsv/default`) |
| dinit | `dinitctl enable NetworkManager` |

Apply to both `sshd` and `NetworkManager` during chroot config.

## Repo setup (required BEFORE pacstrap)

Artix's mirrors are not Arch's. Install the keyring + mirrorlist:

```bash
pacman-key --init
pacman-key --populate archlinux artix
pacman -Sy artix-keyring artix-mirrorlist
```

## Arch-package overlay (optional)

Artix packages a subset of Arch packages with systemd bits removed. For
wider package availability, enable Arch's repos via the `artix-archlinux-
support` package, but be careful — Arch packages may have systemd deps.

## Distro-specific pitfalls

- **Every `systemctl` call fails.** Agent MUST branch on init type during
  the entire setup phase, not just at install time. Check the chosen
  init before any service-related command.
- **User-provided init choice matters.** The wizard should capture
  "OpenRC / runit / s6 / dinit" as an explicit preference; default to
  OpenRC if unset (most common in Artix's user base).
- **elogind substitutes for systemd-logind.** Required for graphical
  sessions. Agent installs `elogind` + the init-specific variant
  (`elogind-openrc`, `elogind-runit`, etc.).
- **AUR builds sometimes assume systemd.** PKGBUILDs that try
  `systemctl enable` break on Artix. Agent handles: strip systemd
  directives from post-install scripts or use a systemd-less fork if
  one exists.
- **polkit-rules** — some D-Bus interactions need polkit; install
  `polkit-elogind` / `polkit-runit` equivalents.
