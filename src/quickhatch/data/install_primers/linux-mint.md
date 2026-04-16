# Linux Mint card

Family: **debian** — uses `families/debian-family.md` as the base.
Derived from Ubuntu LTS, so the base install procedure is the Ubuntu path
in that primer.

## Per-distro values

| Key | Value |
|---|---|
| Live user | `mint` (no password) |
| Release codename | Matches its Ubuntu base: Mint 22 "Wilma" → `noble` (Ubuntu 24.04); Mint 21.x → `jammy` (22.04) |
| Mirror | Ubuntu's: `http://archive.ubuntu.com/ubuntu` |
| Extra repo | `deb http://packages.linuxmint.com <MINT_CODENAME> main upstream import backport` (Mint's own repo, for mint-* packages) |
| Extra key | `apt-key adv --keyserver keyserver.ubuntu.com --recv A6616109451BBBF2` |
| Desktop metapackage | `mint-meta-cinnamon` (default) / `mint-meta-xfce` / `mint-meta-mate` |
| Installer | Ubiquity (Ubuntu-compatible autoinstall YAML works) |
| Mint codename | `wilma` (22), `victoria` (21.2), `vanessa` (21.1), `vera` (21), etc. |
| sudo group | `sudo` |

## Installer-specific path

Use the Ubuntu card's cloud-init `user-data` format — Mint's Ubiquity
accepts it identically.

If using Path B (debootstrap), add the Mint repo + key INSIDE the chroot
before installing the `mint-meta-*` metapackage:

```bash
echo 'deb http://packages.linuxmint.com wilma main upstream import backport' \
  > /etc/apt/sources.list.d/mint.list
apt-key adv --keyserver keyserver.ubuntu.com --recv A6616109451BBBF2
apt update
apt install -y mint-meta-cinnamon
```

## Distro-specific pitfalls

- **No snap by default** — Mint actively blocks snapd. Firefox is a deb via
  Mozilla's own repo (Mint handles this). Don't `apt install snapd` unless
  user explicitly asks.
- **Mint lags Ubuntu** — packages in current Ubuntu may not exist in Mint's
  base yet. Check versions before assuming.
- **PPA codenames** — use the Ubuntu base codename (`jammy`, `noble`) in
  PPAs, not the Mint codename (`wilma`).
- **MintTools nag** — Update Manager will offer NVIDIA drivers and
  available updates. Harmless if agent pre-configured them.
- **Codecs preinstalled** — no need for the Ubuntu-style
  `ubuntu-restricted-extras` step.
