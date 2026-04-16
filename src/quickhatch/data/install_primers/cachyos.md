# CachyOS card

Family: **arch** — uses `families/arch-family.md` as the base procedure.
Arch-based, optimized kernels (BORE/EEVDF schedulers, x86-64-v3/v4 compiled
packages, BBR congestion control). Gamer/performance focus.

## Per-distro values

| Key | Value |
|---|---|
| Live env | KDE Plasma as `cachyos` user |
| Extra repos | `[cachyos]`, `[cachyos-v3]`, `[cachyos-v4]` (added by `cachyos-repo.sh`) |
| Pacstrap extras | `linux-cachyos linux-cachyos-headers cachyos-settings cachyos-keyring` **instead of** `linux` + `linux-firmware` |
| Installer alternative | Calamares; prefer pacstrap path from family primer |
| AUR helper | `paru` preinstalled in target |
| sudo group | `wheel` |

## Repo setup (add BEFORE pacstrap)

```bash
curl https://mirror.cachyos.org/cachyos-repo.tar.xz | tar xJ
cd cachyos-repo
sudo ./cachyos-repo.sh        # adds repos + signing keys to live env
```

Then in the chroot, re-run the script against `/mnt` or manually copy
`/etc/pacman.conf` + `/etc/pacman.d/cachyos-mirrorlist` into the target.

## Kernel choice

| Package | Notes |
|---|---|
| `linux-cachyos` | BORE scheduler, x86-64-v3. Default desktop/gaming. |
| `linux-cachyos-bmq` | BMQ scheduler variant |
| `linux-cachyos-eevdf` | EEVDF upstream-flavored |
| `linux-cachyos-lts` | CachyOS patches on LTS kernel |
| `linux-cachyos-hardened` | Hardened variant |
| `linux-cachyos-rt` | Realtime |

Default: `linux-cachyos`. For rock-solid long-term: `linux-cachyos-lts`.

## Distro-specific pitfalls

- **x86-64-v3 CPU requirement** — CachyOS default packages need Haswell
  (2013+) / Piledriver or newer. Agent checks `grep avx2 /proc/cpuinfo` —
  if missing, falls back to `linux` (stock Arch kernel) and skips
  `[cachyos-v3]` / `[cachyos-v4]` repos.
- **Mirrors** — CachyOS mirrors sometimes fail; swap to another in
  `/etc/pacman.d/cachyos-mirrorlist`.
- **systemd-boot vs GRUB** — default is GRUB. Switch to systemd-boot with
  `bootctl install` + loader entry if user prefers.
- **Flashy GRUB theme** — disable animation with `GRUB_TIMEOUT_STYLE=hidden`
  in `/etc/default/grub` if user wants faster boot.
