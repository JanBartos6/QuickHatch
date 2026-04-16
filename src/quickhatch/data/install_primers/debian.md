# Debian stable install primer

Conservative, long-release-cycle Debian. Installer is `debian-installer`
(d-i), which supports preseed files for unattended install.

## Live environment

- Debian netinst ISO boots to `debian-installer` boot menu.
- Live ISOs (GNOME/KDE/XFCE) boot to desktop; can run installer from there.
- TTY as `root` with no password, or `user` with password `live`.

## Network

- Ethernet auto via dhclient / NetworkManager.
- WiFi: `nmcli device wifi connect <SSID> password <PSK>` (live), or during
  d-i select the WiFi device and enter credentials.

## Approach — preseed (preferred)

Drop a preseed file on the live env, run d-i against it.

```
# /tmp/preseed.cfg
d-i debian-installer/locale string en_US.UTF-8
d-i keyboard-configuration/xkb-keymap select us

# Network
d-i netcfg/get_hostname string <HOSTNAME>
d-i netcfg/get_domain string localdomain

# Clock + timezone
d-i clock-setup/utc boolean true
d-i time/zone string <ZONE>
d-i clock-setup/ntp boolean true

# Partitioning (wipe disk, single root)
d-i partman-auto/disk string /dev/<DISK_SHORT>
d-i partman-auto/method string regular
d-i partman-auto/choose_recipe select atomic
d-i partman-partitioning/confirm_write_new_label boolean true
d-i partman/choose_partition select finish
d-i partman/confirm boolean true
d-i partman/confirm_nooverwrite boolean true

# Users
d-i passwd/root-password password <ROOT_PASSWORD>
d-i passwd/root-password-again password <ROOT_PASSWORD>
d-i passwd/user-fullname string <REAL_NAME>
d-i passwd/username string <USER>
d-i passwd/user-password password <PASSWORD>
d-i passwd/user-password-again password <PASSWORD>
d-i user-setup/allow-password-weak boolean true

# Packages
tasksel tasksel/first multiselect standard, ssh-server
d-i pkgsel/include string openssh-server sudo network-manager curl

# Bootloader
d-i grub-installer/only_debian boolean true
d-i grub-installer/bootdev string default

# Late commands: add user to sudo, install SSH key
d-i preseed/late_command string \
  in-target usermod -aG sudo <USER>; \
  in-target mkdir -p /home/<USER>/.ssh; \
  echo "<PUBKEY>" > /target/home/<USER>/.ssh/authorized_keys; \
  in-target chown -R <USER>:<USER> /home/<USER>/.ssh; \
  in-target chmod 700 /home/<USER>/.ssh; \
  in-target chmod 600 /home/<USER>/.ssh/authorized_keys; \
  in-target systemctl enable ssh NetworkManager

# Finish
d-i finish-install/reboot_in_progress note
```

Pass at kernel cmdline: `preseed/file=/tmp/preseed.cfg auto=true priority=critical`

## Alternative — debootstrap from live

Same as Ubuntu primer but with Debian release:

```bash
sudo debootstrap --arch=amd64 bookworm /mnt \
  http://deb.debian.org/debian
# bookworm = stable as of 2024; use trixie for testing.
```

Then chroot + install `linux-image-amd64` + `grub-efi-amd64` + `systemd-sysv`
(not present by default in debootstrap).

## Common pitfalls

- **`systemd-sysv` missing** — debootstrap doesn't install it. Without it,
  you get no init system. Always add to the package list.
- **`sudo` not installed by default** — Debian's minimum install has `su` but
  not `sudo`. Add explicitly.
- **Firmware missing** — debian netinst doesn't ship non-free firmware.
  Either use the "firmware" variant of the ISO or add `contrib non-free
  non-free-firmware` to `/etc/apt/sources.list` post-install.
- **Backports** — for newer kernels on stable, enable
  `deb http://deb.debian.org/debian bookworm-backports main` and install
  `linux-image-amd64/bookworm-backports`.
