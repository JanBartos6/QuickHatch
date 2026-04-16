# Fedora Workstation install primer

Point-release, 6-month cadence, GNOME default. The live ISO ships Anaconda,
Red Hat's installer. Agent drives it headlessly via a kickstart file.

## Live environment

- Boots to a `liveuser` account, auto-logged-in to GNOME.
- Drop to TTY: Ctrl+Alt+F3, log in as `liveuser` (no password).
- Tools available: `anaconda`, `parted`, `dnf`.

## Network

- Ethernet auto-connects via NetworkManager.
- WiFi: `nmcli device wifi connect <SSID> password <PSK>`.

## Approach

Fedora's installer (Anaconda) is the cleanest way. Generate a kickstart file
describing the install, then run Anaconda in text mode against it.

## Kickstart file

```bash
cat > /tmp/qh.ks <<'KICKSTART'
text
lang en_US.UTF-8
keyboard us
timezone <ZONE> --utc

# Root password (hashed). Generate with `openssl passwd -6 '<ROOT_PASSWORD>'`.
rootpw --iscrypted <ROOT_PASSWORD_HASH>

# User (sudo via wheel)
user --name=<USER> --groups=wheel --iscrypted --password=<PASSWORD_HASH>

# Disk: wipe and auto-partition
clearpart --all --drives=<DISK_SHORT>
autopart --type=plain
bootloader --location=mbr --boot-drive=<DISK_SHORT>

# Network: DHCP, keep hostname
network --bootproto=dhcp --device=link --activate --hostname=<HOSTNAME>

# SSH
services --enabled=sshd,NetworkManager
sshkey --username=<USER> "<PUBKEY>"

%packages
@core
@standard
@workstation-product
openssh-server
sudo
NetworkManager
%end

reboot
KICKSTART
```

`<DISK_SHORT>` is just the device name without `/dev/`, e.g. `nvme0n1`.

## Run the installer

```bash
# Agent runs this; anaconda takes over the console + does everything
sudo anaconda --kickstart=/tmp/qh.ks
```

Anaconda performs partition, rpm install, bootloader, user creation, first
boot config. It reboots at the end.

## Post-reboot

Nothing extra: kickstart enabled sshd and pre-installed the user's SSH key
via the `sshkey` directive. Agent reconnects as `<USER>@<IP>`.

## Alternative: Fedora Everything ISO (bootstrap from chroot)

If Anaconda fails, fall back to a chroot install (slower but more control):

```bash
DISK=/dev/nvme0n1
parted -s "$DISK" mklabel gpt \
  mkpart ESP fat32 1MiB 513MiB set 1 esp on \
  mkpart primary ext4 513MiB 100%
mkfs.fat -F32 "${DISK}p1"
mkfs.ext4 -F "${DISK}p2"
mount "${DISK}p2" /mnt
mkdir /mnt/boot
mount "${DISK}p1" /mnt/boot

sudo dnf --installroot=/mnt --releasever=41 install -y \
  @core @standard kernel dnf sudo NetworkManager openssh-server grub2 \
  grub2-efi-x64 efibootmgr shim-x64

# Configure + chroot as in Arch primer, using dnf/systemctl equivalents.
```

## Common pitfalls

- **Hashed passwords** — kickstart's `--iscrypted` needs `crypt(3)` SHA-512
  hashes, not plaintext. Agent generates via `openssl passwd -6`.
- **Fedora version** — set `releasever` in dnf commands to the current Fedora
  version (41 as of this file; the agent reads `/etc/fedora-release` on the
  live env to confirm).
- **Secure Boot** — Fedora's shim is signed, so Secure Boot generally works
  out of the box. If it fails, agent can guide disabling SB.
- **Kickstart file location** — some live envs expect it at
  `/run/install/ks.cfg` or passed via `inst.ks=` kernel param. The `--kickstart`
  flag is the most portable.
