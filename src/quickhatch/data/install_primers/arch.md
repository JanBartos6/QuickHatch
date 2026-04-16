# Arch Linux card

Family: **arch** — uses `families/arch-family.md` as the base procedure.
Arch IS the family reference; this card is just the distro-specific values
and any deviations.

## Per-distro values

| Key | Value |
|---|---|
| Live env | Root shell, no password |
| Extra repos | None (vanilla) |
| Pacstrap base | `base linux linux-firmware sudo nano networkmanager openssh grub efibootmgr sof-firmware <ucode>` |
| Pacstrap extras | None |
| Installer alternative | `archinstall` (interactive TUI) — but agent uses the manual pacstrap path from the family primer |
| AUR helper | None pre-installed; agent can build `yay` from AUR if needed |
| sudo group | `wheel` |

## Distro-specific pitfalls

None beyond the family-level ones (Secure Boot, disk confirmation,
pacstrap mirror stalls). Arch is the family's reference case.

## Notes

- For a friendlier Arch, pick **EndeavourOS** (same base, added quality-of-
  life) or **CachyOS** (performance-tuned kernels).
- For GNOME / KDE / Hyprland etc., agent installs the DE metapackage
  after the base is running (`pacman -Sy plasma kde-applications`, etc.)
  — not during pacstrap, to keep the base minimal and debuggable.
