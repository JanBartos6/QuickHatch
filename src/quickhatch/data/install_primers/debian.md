# Debian stable card

Family: **debian** — uses `families/debian-family.md` as the base install
procedure.

## Per-distro values

| Key | Value |
|---|---|
| Live user | `user` (password `live`) or graphical-installer boot |
| Release codename | `bookworm` (stable), `trixie` (testing) |
| Mirror | `http://deb.debian.org/debian` |
| Extra repos | None required. For WiFi/NVIDIA firmware add `contrib non-free non-free-firmware` to sources.list |
| Desktop metapackage | `task-gnome-desktop` (GNOME default) / `task-kde-desktop` / `task-xfce-desktop` / `task-cinnamon-desktop` |
| Installer | `debian-installer` (d-i) with preseed |
| sudo group | `sudo` |

## Installer-specific path (d-i with preseed)

If using Path A (official installer) instead of Path B (debootstrap from
family primer), here's the preseed:

```
d-i debian-installer/locale string en_US.UTF-8
d-i keyboard-configuration/xkb-keymap select us
d-i netcfg/get_hostname string <HOSTNAME>
d-i clock-setup/utc boolean true
d-i time/zone string <ZONE>

d-i partman-auto/disk string /dev/<DISK_SHORT>
d-i partman-auto/method string regular
d-i partman-auto/choose_recipe select atomic
d-i partman-partitioning/confirm_write_new_label boolean true
d-i partman/choose_partition select finish
d-i partman/confirm boolean true
d-i partman/confirm_nooverwrite boolean true

d-i passwd/root-password password <ROOT_PASSWORD>
d-i passwd/root-password-again password <ROOT_PASSWORD>
d-i passwd/user-fullname string <REAL_NAME>
d-i passwd/username string <USER>
d-i passwd/user-password password <PASSWORD>
d-i passwd/user-password-again password <PASSWORD>

tasksel tasksel/first multiselect standard, ssh-server, gnome-desktop
d-i pkgsel/include string openssh-server sudo network-manager curl

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

Boot d-i with `preseed/file=/tmp/preseed.cfg auto=true priority=critical`.

## Distro-specific pitfalls

- **Firmware missing on netinst** — default netinst doesn't bundle non-free
  firmware. Either use the "firmware" variant ISO or add the `non-free-firmware`
  component to sources.list after install.
- **`sudo` not installed by default** — always add it to the package list.
- **Kernel is conservative** — for very new hardware, enable
  `bookworm-backports` and install `linux-image-amd64/bookworm-backports`.
