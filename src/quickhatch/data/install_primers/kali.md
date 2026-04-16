# Kali Linux install primer

Debian testing-based, rolling release, built around a curated suite of
pentesting + red-team tools. Default DE is XFCE; KDE / GNOME / i3 / minimal
flavors available. Supports d-i preseed for unattended install.

## Live environment

- Official ISO boots to a Kali boot menu: Live (amd64), Install, Graphical
  Install, Forensic Mode, etc.
- Live session user: `kali` / `kali` (recent ISOs). Older: `root` / `toor`.
- Sudo works without asking for a password in live mode.

## Network

- Ethernet auto via NetworkManager.
- WiFi: `nmcli device wifi connect <SSID> password <PSK>` in live.
- For pentesting setups (monitor mode, injection): the agent shouldn't
  touch adapter state during install — only after user signs off.

## Approach — d-i preseed (same shape as Debian, Kali-flavored)

```
# /tmp/preseed.cfg
d-i debian-installer/locale string en_US.UTF-8
d-i keyboard-configuration/xkb-keymap select us

d-i netcfg/get_hostname string <HOSTNAME>
d-i netcfg/get_domain string localdomain

d-i clock-setup/utc boolean true
d-i time/zone string <ZONE>
d-i clock-setup/ntp boolean true

# Mirror: Kali's own mirror
d-i mirror/country string manual
d-i mirror/http/hostname string http.kali.org
d-i mirror/http/directory string /kali
d-i mirror/http/proxy string

# Partitioning (wipe disk, single root)
d-i partman-auto/disk string /dev/<DISK_SHORT>
d-i partman-auto/method string regular
d-i partman-auto/choose_recipe select atomic
d-i partman-partitioning/confirm_write_new_label boolean true
d-i partman/choose_partition select finish
d-i partman/confirm boolean true
d-i partman/confirm_nooverwrite boolean true

# Users (Kali 2020.1+ uses regular user + sudo, not root-login)
d-i passwd/root-login boolean false
d-i passwd/make-user boolean true
d-i passwd/user-fullname string <REAL_NAME>
d-i passwd/username string <USER>
d-i passwd/user-password password <PASSWORD>
d-i passwd/user-password-again password <PASSWORD>
d-i user-setup/allow-password-weak boolean true

# Packages — pick one of Kali's metapackages
# kali-linux-default  = ~600 top tools (the default)
# kali-linux-large    = ~every tool
# kali-linux-everything = literally everything, biggest
# kali-linux-core     = minimal set
# kali-linux-headless = no GUI (for SSH-only boxes / VMs)
tasksel tasksel/first multiselect standard, ssh-server
d-i pkgsel/include string kali-linux-default openssh-server sudo network-manager

# DE choice (only if not headless): comment out all for defaults
# kali-desktop-xfce  / kali-desktop-kde  / kali-desktop-gnome / kali-desktop-i3

# Bootloader
d-i grub-installer/only_debian boolean true
d-i grub-installer/bootdev string default

# Late commands: sudo group + SSH key
d-i preseed/late_command string \
  in-target usermod -aG sudo <USER>; \
  in-target mkdir -p /home/<USER>/.ssh; \
  echo "<PUBKEY>" > /target/home/<USER>/.ssh/authorized_keys; \
  in-target chown -R <USER>:<USER> /home/<USER>/.ssh; \
  in-target chmod 700 /home/<USER>/.ssh; \
  in-target chmod 600 /home/<USER>/.ssh/authorized_keys; \
  in-target systemctl enable ssh NetworkManager

d-i finish-install/reboot_in_progress note
```

Boot d-i with kernel cmdline `preseed/file=/tmp/preseed.cfg auto=true priority=critical`.

## Alternative — debootstrap with Kali repos

If d-i isn't available or behaves oddly:

```bash
sudo debootstrap --arch=amd64 kali-rolling /mnt http://http.kali.org/kali

for f in dev dev/pts proc sys run; do sudo mount --bind /$f /mnt/$f; done

sudo chroot /mnt /bin/bash <<'EOF'
# Kali repo + signing key
cat > /etc/apt/sources.list <<SOURCES
deb http://http.kali.org/kali kali-rolling main contrib non-free non-free-firmware
SOURCES
wget -qO- https://archive.kali.org/archive-key.asc | apt-key add -
apt update

# Core + kernel + bootloader
apt install -y linux-image-amd64 systemd-sysv sudo grub-efi-amd64 efibootmgr \
  openssh-server network-manager locales

# Kali tool metapackage (pick one — default is medium)
apt install -y kali-linux-default     # ~600 tools, typical desktop
# apt install -y kali-linux-large       # full arsenal
# apt install -y kali-linux-headless    # SSH-only setup

# Standard post-install: locale, hostname, timezone, user, ssh key, grub
# (matches the Debian primer almost exactly)
EOF
```

## Metapackage cheat sheet

| Metapackage              | Size  | Contents                                    |
|--------------------------|-------|---------------------------------------------|
| `kali-linux-core`        | ~small| Base system + essential Kali pieces         |
| `kali-linux-headless`    | ~med  | CLI-only pentesting tools (no GUI)          |
| `kali-linux-default`     | ~large| Top ~600 tools + XFCE (installer default)   |
| `kali-linux-large`       | ~xl   | Near-complete tool set                      |
| `kali-linux-everything`  | ~xxl  | Literally every Kali package (100+ GB)      |

QuickHatch agents should default to `kali-linux-default`. Users wanting the
full arsenal can pick `large`; headless users (VM, cloud) can pick `headless`.

## Undercover mode

Kali ships `kali-undercover` — a script that retheme the XFCE desktop to look
like Windows 10, for situations where "that Linux hacker laptop" would
attract attention. Run with `kali-undercover`; toggle back with the same
command. Agent can enable it post-install on request.

## Common pitfalls

- **WiFi monitor-mode drivers** — Kali ships realtek/broadcom drivers
  patched for injection. Don't overwrite with mainline equivalents.
- **Kali-specific repos** — never mix with Debian stable repos. The
  package versions diverge; mixing breaks apt resolver. Kali is rolling.
- **The "non-free-firmware" component** — required for most WiFi + NVIDIA.
  Ensure sources.list includes it.
- **Default user is not root (since 2020.1)** — don't assume root login.
  Kali moved to a normal user + sudo model to match other distros.
- **Big install size** — `kali-linux-large` is ~40 GB; `everything` is
  ~100 GB+. Agent should warn the user if they pick these with a small
  disk.
- **Secure Boot** — Kali's shim is signed for most releases, but some
  tools (e.g. custom kernel modules for injection) require SB disabled.
  Tell the user upfront if they plan to use monitor-mode Wi-Fi.
