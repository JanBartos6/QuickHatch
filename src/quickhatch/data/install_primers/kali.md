# Kali Linux card

Family: **debian** — uses `families/debian-family.md` as the base.
Debian testing-based, rolling, purpose-built pentesting distro.

## Per-distro values

| Key | Value |
|---|---|
| Live user | `kali` (password `kali`) on recent ISOs; older: `root` / `toor` |
| Release codename | `kali-rolling` |
| Mirror | `http://http.kali.org/kali` |
| Extra components | `main contrib non-free non-free-firmware` (all needed for WiFi injection + misc tools) |
| Signing key | `wget -qO- https://archive.kali.org/archive-key.asc \| apt-key add -` |
| Desktop metapackage | `kali-linux-default` (default, ~600 tools + XFCE) |
| Installer | `debian-installer` with Kali preseed (d-i path) |
| sudo group | `sudo` |

## Kali metapackage cheat-sheet

| Metapackage | Size | Contents |
|---|---|---|
| `kali-linux-core` | small | Base + essential Kali pieces |
| `kali-linux-headless` | medium | CLI pentesting tools, no GUI |
| `kali-linux-default` | large | Top ~600 tools + XFCE (installer default) |
| `kali-linux-large` | xl | Near-complete tool set |
| `kali-linux-everything` | xxl | Literally every Kali package (100+ GB) |

Default agent choice: `kali-linux-default`. Headless for VMs/cloud;
large/everything only if disk + user intent justify.

## Installer-specific path (d-i with preseed)

```
d-i debian-installer/locale string en_US.UTF-8
d-i keyboard-configuration/xkb-keymap select us
d-i netcfg/get_hostname string <HOSTNAME>
d-i clock-setup/utc boolean true
d-i time/zone string <ZONE>
d-i clock-setup/ntp boolean true

d-i mirror/country string manual
d-i mirror/http/hostname string http.kali.org
d-i mirror/http/directory string /kali

d-i partman-auto/disk string /dev/<DISK_SHORT>
d-i partman-auto/method string regular
d-i partman-auto/choose_recipe select atomic
d-i partman-partitioning/confirm_write_new_label boolean true
d-i partman/choose_partition select finish
d-i partman/confirm boolean true
d-i partman/confirm_nooverwrite boolean true

d-i passwd/root-login boolean false
d-i passwd/make-user boolean true
d-i passwd/user-fullname string <REAL_NAME>
d-i passwd/username string <USER>
d-i passwd/user-password password <PASSWORD>
d-i passwd/user-password-again password <PASSWORD>

tasksel tasksel/first multiselect standard, ssh-server
d-i pkgsel/include string kali-linux-default openssh-server sudo network-manager

d-i grub-installer/only_debian boolean true
d-i grub-installer/bootdev string default

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

Boot with `preseed/file=/tmp/preseed.cfg auto=true priority=critical`.

## Distro-specific pitfalls

- **Post-2020.1 model** — Kali moved to normal-user + sudo, not root login.
  Don't assume `root`.
- **WiFi monitor-mode drivers** — Kali ships patched realtek/broadcom
  drivers for injection. Don't overwrite with mainline equivalents.
- **Never mix with Debian stable repos** — Kali is rolling, versions
  diverge sharply. Mixing breaks apt's resolver.
- **non-free-firmware required** — explicitly enable the component; most
  WiFi + NVIDIA won't work without it.
- **kali-undercover** — script that retheme XFCE to look like Windows 10.
  Run `kali-undercover` to toggle. Useful for physical-world stealth.
- **Install size** — `kali-linux-large` is ~40 GB; `everything` is ~100 GB+.
  Warn user if disk is small.
- **Secure Boot** — signed shim works for most releases, but monitor-mode
  kernel modules may require SB disabled. Tell user upfront if they plan
  to use injection.
