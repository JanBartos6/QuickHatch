# Testing QuickHatch in a VirtualBox VM

The safest way to try QuickHatch is against a disposable VirtualBox VM. This guide sets up an Arch Linux VM you can migrate into — snapshot before each test so you can replay from a known state.

## Prerequisites

- VirtualBox 7.x installed and on PATH (or known path — examples use `G:\VirtualBox\VBoxManage.exe`)
- ~10 GB free disk for the VM
- Your SSH public key at `~/.ssh/id_ed25519.pub`

## 1. Create the VM

```bash
VBOX="G:/VirtualBox/VBoxManage.exe"

# Create
"$VBOX" createvm --name "QuickHatch-Arch" --ostype "ArchLinux_64" --register --basefolder "G:/VMs"

# Configure
"$VBOX" modifyvm "QuickHatch-Arch" \
  --memory 8192 --cpus 4 --vram 256 \
  --graphicscontroller vmsvga \
  --firmware efi \
  --boot1 dvd --boot2 disk \
  --nic1 nat \
  --nat-pf1 "ssh,tcp,,2222,,22"

# Disk
"$VBOX" createmedium disk \
  --filename "G:/VMs/QuickHatch-Arch/QuickHatch-Arch.vdi" \
  --size 40960 --format VDI
"$VBOX" storagectl "QuickHatch-Arch" --name "SATA" --add sata --controller IntelAhci
"$VBOX" storageattach "QuickHatch-Arch" --storagectl "SATA" --port 0 --device 0 \
  --type hdd --medium "G:/VMs/QuickHatch-Arch/QuickHatch-Arch.vdi"

# ISO (download Arch from https://archlinux.org/download/)
"$VBOX" storagectl "QuickHatch-Arch" --name "IDE" --add ide
"$VBOX" storageattach "QuickHatch-Arch" --storagectl "IDE" --port 0 --device 0 \
  --type dvddrive --medium "/path/to/archlinux-x86_64.iso"
```

## 2. Install Arch

Start the VM, boot into the live ISO, and either:

- Run `archinstall` for an interactive install, or
- Run a pacstrap-based script (one example lives in this repo at `.claude/arch-install.sh` during testing — copy its logic)

Key requirements for the installed system:
- UEFI-mode install on `/dev/sda1` (512MB FAT32 EFI) + `/dev/sda2` (ext4/btrfs root)
- Install: `base base-devel linux linux-firmware grub efibootmgr networkmanager openssh sudo`
- `systemctl enable NetworkManager sshd`
- Set a root password
- Inject your SSH public key into `/root/.ssh/authorized_keys` (mode 600, dir mode 700)
- Reboot

## 3. Snapshot

After a clean install with SSH working:

```bash
# Shut down the VM first
"$VBOX" controlvm "QuickHatch-Arch" poweroff
"$VBOX" snapshot "QuickHatch-Arch" take "fresh-install" \
  --description "Arch installed, SSH ready, before agent runs"
```

To revert later:

```bash
"$VBOX" snapshot "QuickHatch-Arch" restore "fresh-install"
```

## 4. Run QuickHatch against the VM

1. Start the VM (it should boot to a login prompt with SSH running).
2. Verify SSH: `ssh -p 2222 -i ~/.ssh/id_ed25519 root@127.0.0.1 "echo ok"`
3. Launch QuickHatch GUI: `python -m quickhatch --gui`
4. Go through the wizard:
   - AI Setup: pick LM Studio / Claude API / whatever
   - Preferences: any
   - System Scan: your real Windows system gets scanned (can use `--demo` to skip)
   - Analyze: the AI generates the migration plan
5. **Remote Setup** step: enter `127.0.0.1:2222` and `root`, click **Launch agent**.
6. Watch the agent work through the plan via SSH.

## 5. Verify the result

```bash
ssh -p 2222 -i ~/.ssh/id_ed25519 root@127.0.0.1 \
  "pacman -Q | wc -l; systemctl is-enabled sddm; cat /etc/xdg/kdeglobals 2>/dev/null | head -10"
```

Reboot the VM and log in to see the actual desktop.

## Common issues

- **VirtualBox mouse doesn't work in KDE Wayland login (SDDM):** install Guest Additions, or configure SDDM for autologin during testing.
- **SDDM hides `root`:** create a regular user with sudo rights (`useradd -m -G wheel honzi`) and log in as that.
- **Agent hangs on disk full (live ISO):** expand the tmpfs cowspace first with `mount -o remount,size=4G /run/archiso/cowspace`. The agent handles this automatically if the plan mentions it.
- **LM Studio "Channel Error":** prompt exceeded the model's context window. Increase the context length in LM Studio or use a smaller prompt.
