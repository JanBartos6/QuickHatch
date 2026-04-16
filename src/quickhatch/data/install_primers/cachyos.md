# CachyOS install primer

Arch-based, optimized kernels (BORE/EEVDF schedulers, x86-64-v3/v4 compiled
packages, BBR congestion control). Gamer/perf focus. Calamares installer.

## Live environment

- Boots to KDE Plasma (default) as `cachyos` user.
- Calamares on the desktop. CLI automation is supported via Calamares
  config files but not as clean as Arch's pacstrap.

## Approach — pacstrap (recommended, same as Arch)

Because CachyOS is Arch, the cleanest install from the live env is
**pacstrap** pointing at CachyOS repos. Same procedure as Arch primer,
with these changes:

```bash
# Pull in CachyOS's repositories first
curl https://mirror.cachyos.org/cachyos-repo.tar.xz | tar xJ
cd cachyos-repo
sudo ./cachyos-repo.sh   # adds repos + signing keys to the live env

# Partition, mkfs, mount (same as Arch primer)

# Pacstrap with CachyOS-optimized packages
pacstrap -K /mnt base linux-cachyos linux-cachyos-headers \
  linux-firmware sudo networkmanager openssh grub efibootmgr \
  sof-firmware intel-ucode cachyos-settings cachyos-keyring

genfstab -U /mnt >> /mnt/etc/fstab

# Chroot and configure (same as Arch primer, plus enable CachyOS repos)
arch-chroot /mnt /bin/bash <<'EOF'
# Copy CachyOS repo config to the new system
# (alternatively run `cachyos-repo.sh` inside chroot)

# ... standard Arch post-install: timezone, locale, hostname, user, ssh key,
# grub-install, grub-mkconfig — all identical to Arch primer
EOF
```

## Alternative — Calamares with unattended config

```bash
# Calamares supports an unattended mode via --config
sudo calamares --config /tmp/calamares-auto.conf
# ... the config file format is per-module, documented at
# https://github.com/calamares/calamares/wiki
```

Calamares automation is fiddly; pacstrap is cleaner.

## Kernel choice

CachyOS ships multiple kernel variants:

| Package            | Notes                                        |
|--------------------|----------------------------------------------|
| `linux-cachyos`    | BORE scheduler, x86-64-v3 compiled. Default. |
| `linux-cachyos-bmq`| BMQ scheduler variant.                       |
| `linux-cachyos-eevdf` | EEVDF scheduler (upstream-flavored).      |
| `linux-cachyos-lts`| CachyOS's patches on top of LTS kernel.      |
| `linux-cachyos-hardened` | Hardened variant.                      |
| `linux-cachyos-rt` | Realtime.                                    |

For desktop/gaming: `linux-cachyos` (BORE). For rock-solid: `linux-cachyos-lts`.

## Common pitfalls

- **x86-64-v3 requirement** — CachyOS's default packages require CPUs from
  ~2013 onwards (Haswell / Piledriver). Older CPUs need the generic variant.
  Agent checks `cat /proc/cpuinfo | grep flags | head -1` for `avx2`; if
  missing, install `linux` instead of `linux-cachyos`.
- **Mirrors** — CachyOS repos sometimes have outages; agent should switch to
  a different mirror (`/etc/pacman.d/cachyos-mirrorlist`) if pacstrap stalls.
- **systemd-boot vs GRUB** — default is GRUB. If user prefers systemd-boot,
  replace the grub-install block with `bootctl install` + a loader entry.
- **Animated GRUB theme** — CachyOS ships a flashy GRUB theme. Agent can
  disable via `/etc/default/grub` → `GRUB_TIMEOUT_STYLE=hidden`.
