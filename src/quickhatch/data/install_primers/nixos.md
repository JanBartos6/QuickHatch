# NixOS install primer

Declarative, reproducible. The entire system config is one `.nix` file. Great
fit for QuickHatch: the whole install collapses to "write configuration.nix
then run nixos-install".

## Live environment

- Minimal ISO: root shell with `nix-env`, `parted`, `nixos-install`, `mkfs.*`.
- Graphical ISO: Calamares installer (skip; use CLI instead for automation).

## Network

- Ethernet: auto.
- WiFi: `wpa_supplicant` or `iwctl station wlan0 connect <SSID>`.

## Partition + format

```bash
DISK=/dev/nvme0n1
parted -s "$DISK" mklabel gpt \
  mkpart ESP fat32 1MiB 513MiB set 1 esp on \
  mkpart primary ext4 513MiB 100%
mkfs.fat -F32 -n boot "${DISK}p1"
mkfs.ext4 -L nixos "${DISK}p2"
mount /dev/disk/by-label/nixos /mnt
mkdir -p /mnt/boot
mount /dev/disk/by-label/boot /mnt/boot
```

## Generate + edit configuration.nix

```bash
nixos-generate-config --root /mnt
```

Now edit `/mnt/etc/nixos/configuration.nix` — overwrite with something like:

```nix
{ config, pkgs, lib, ... }:

{
  imports = [ ./hardware-configuration.nix ];

  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = true;

  networking.hostName = "<HOSTNAME>";
  networking.networkmanager.enable = true;

  time.timeZone = "<ZONE>";
  i18n.defaultLocale = "en_US.UTF-8";

  users.users.<USER> = {
    isNormalUser = true;
    description = "<REAL_NAME>";
    extraGroups = [ "wheel" "networkmanager" "audio" "video" ];
    shell = pkgs.bash;
    openssh.authorizedKeys.keys = [
      "<PUBKEY>"
    ];
  };

  # Allow <USER> sudo without password (optional, safer to require)
  security.sudo.wheelNeedsPassword = true;

  services.openssh = {
    enable = true;
    settings.PasswordAuthentication = false;
    settings.PermitRootLogin = "no";
  };

  # GUI: GNOME default, agent swaps to user's chosen DE later
  services.xserver.enable = true;
  services.displayManager.gdm.enable = true;
  services.desktopManager.gnome.enable = true;

  environment.systemPackages = with pkgs; [
    git curl wget vim sudo
  ];

  system.stateVersion = "24.11";   # matches the NixOS release in use
}
```

## Install + reboot

```bash
# Set root password (NixOS won't boot without one)
nixos-install --no-root-passwd   # user is already defined above
# or interactively: nixos-install   (prompts for root password)

# If root password wasn't set above, do it now:
nixos-enter --root /mnt -c 'passwd <USER>'
nixos-enter --root /mnt -c 'passwd'   # root password

reboot
```

## Flake-based alternative (modern)

For users who want flake-based config:

```bash
nix-shell -p git
git clone https://github.com/<user>/nix-config /mnt/etc/nixos
# Edit flake.nix + hardware-configuration.nix as needed
nixos-install --flake /mnt/etc/nixos#<hostname>
```

## Post-install agent handoff

After reboot, agent SSHes in as `<USER>@<IP>`. From there, further
configuration is either:

1. **Imperative** — `sudo nixos-rebuild switch` after editing
   `/etc/nixos/configuration.nix` (what the agent does for a desktop
   recipe)
2. **Declarative** — agent writes the full final config (with Hyprland,
   theming, apps etc.) and runs `nixos-rebuild switch --upgrade` once.

The declarative path is cleaner and rolls back on failure.

## Common pitfalls

- **`hardware-configuration.nix`** is generated per machine — DO NOT
  overwrite it, only `configuration.nix`.
- **stateVersion** — must match the NixOS release you're installing.
  Changing it later can break things; set it once at install time.
- **Flake permissions** — if `/mnt/etc/nixos` is cloned as root, users
  can't edit without sudo. `chown -R <USER>:<USER> /mnt/etc/nixos` post
  install if flake-based.
- **Channel vs flake mismatch** — pick one model and stick with it.
- **Unfree packages** — `nixpkgs.config.allowUnfree = true;` in
  configuration.nix or `--impure` on rebuild.
