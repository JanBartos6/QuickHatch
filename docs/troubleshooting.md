# Troubleshooting

Known issues and how to work around them.

## LM Studio

### "Channel Error" mid-run

**Symptom:** LM Studio logs show `[ERROR] Channel Error`. The agent stops mid-setup.

**Cause:** The prompt exceeded the model's context window. LM Studio kills the worker silently when this happens.

**Fix:**
1. In LM Studio, unload the model.
2. Load it again with a bigger context window (Settings → model config → Context Length → 32k or 100k).
3. Re-launch the agent in QuickHatch. It'll start the setup phase over.

### Model loaded but QuickHatch says "no models available"

**Symptom:** The AI Setup step's LM Studio option shows the text-input fallback instead of a dropdown.

**Cause:** QuickHatch queries `http://localhost:1234/v1/models`. Either LM Studio's server isn't running, or it's bound to a different port.

**Fix:**
1. In LM Studio: left sidebar → **Developer** → toggle **Start Server**.
2. Confirm it's on port 1234.
3. Refresh the QuickHatch page.

### Model is slow

**Symptom:** Each agent turn takes 60+ seconds.

**Likely causes:**
- Model too big for your GPU, running on CPU.
- Flash Attention disabled.
- Low VRAM forcing layer offload to CPU.

**Fix:** Check LM Studio's "GPU Offload" setting on the model. If layers are going to CPU, the model will be 10x slower. Either enable full GPU offload or use a smaller model.

## VirtualBox testing

### Can't click anything after logging in to KDE

**Symptom:** Cursor is captured in VM but mouse clicks do nothing. Keyboard input works.

**Cause:** KDE Plasma is running on Wayland, and VirtualBox's VMSVGA driver has known Wayland mouse integration issues without Guest Additions.

**Fix options:**
1. **Enable SDDM autologin** — so you skip the login problem entirely:
   ```bash
   # From host
   ssh -p 2222 -i ~/.ssh/id_ed25519 root@127.0.0.1 \
     "mkdir -p /etc/sddm.conf.d && printf '[Autologin]\nUser=honzi\nSession=plasma\nRelogin=true\n' > /etc/sddm.conf.d/autologin.conf"
   ```
2. **Install Guest Additions** in the VM (`pacman -S --noconfirm virtualbox-guest-utils` and `systemctl enable vboxservice`).
3. **Use X11 session instead of Wayland** — at the SDDM login screen, click the bottom-left "Session" menu and pick "Plasma (X11)".

### SDDM hides the root user

**Symptom:** Login screen says "Select your user and enter password" but shows no users.

**Cause:** SDDM by default filters users with UID < 1000, which hides root.

**Fix:** Create a regular user:
```bash
ssh -p 2222 -i ~/.ssh/id_ed25519 root@127.0.0.1 \
  "useradd -m -G wheel,video,audio,input -s /bin/bash myuser; echo 'myuser:mypassword' | chpasswd"
```

### "VMState=onlinesnapshotting" hangs forever

**Symptom:** Live snapshot takes many minutes, the VM is unresponsive.

**Cause:** Saving 8GB+ of RAM to a slow disk (especially HDD). VirtualBox live snapshots are not great.

**Fix:** Shut down the VM first, then take the snapshot. Or use `--live` only for small-memory VMs.

```bash
# Graceful shutdown first
VBoxManage controlvm QuickHatch-Arch acpipowerbutton
# Wait a bit, then if needed
VBoxManage controlvm QuickHatch-Arch poweroff
# Now snapshot (no --live)
VBoxManage snapshot QuickHatch-Arch take "my-snapshot"
```

### SSH says "REMOTE HOST IDENTIFICATION HAS CHANGED"

**Symptom:** SSH refuses to connect with a big warning after you reinstall/reboot the VM.

**Cause:** The host key on the VM changed (reinstall, snapshot restore, etc.) and your `~/.ssh/known_hosts` still has the old one.

**Fix:**
```bash
ssh-keygen -R "[127.0.0.1]:2222"
```

## Agent behavior

### Agent regenerates the same plan instead of executing it

**Symptom:** Setup agent runs every section prompt again instead of executing SSH commands.

**Cause:** The bridge state didn't carry the analysis suggestions into the setup phase. This happens if you launched the setup agent directly without going through the analysis step first.

**Fix:** Run the full flow: AI Setup → Preferences → Scan → Apps → Additional → Bridge (analysis) → then Remote Setup. The bridge step populates `bridge.suggestions` which the setup step reads.

### Agent keeps retrying the same failed command

**Symptom:** Command fails, agent reasons about it, then runs the exact same command again.

**Cause:** Smaller models sometimes can't diagnose failures from the error output. They see "error" and default to "try again".

**Fix:** Use a bigger model (Gemma 26B minimum, Claude Sonnet ideal). See `docs/model-recommendations.md`.

### Setup agent timeout

**Symptom:** Agent stops mid-install with "timed out" errors.

**Cause:** The default `_ssh_exec` timeout is 300 seconds. Some commands (full `plasma-meta` install, kernel rebuild, etc.) take longer on a slow disk.

**Fix:** Currently requires code change in `gui/server.py`. Increase `timeout=300` in the `_ssh_exec` calls. A config option for this is a reasonable contribution.

## GUI quirks

### Scan step says "No apps detected"

**Symptom:** The Apps & Data step shows no apps after the scan.

**Cause:** The old GUI had a bug where the client-side profile wasn't refreshed after scan. Fixed in the current version; if you still see this, check the browser console.

### "Agent done" button stays disabled

**Symptom:** Agent finished but the Continue button stays disabled.

**Cause:** The `/api/done` call from the agent's thread didn't reach the bridge (e.g. port conflict).

**Fix:** Refresh the page. The profile is checkpointed so you can resume.

## Export bundle

### `quickstart.sh` says "permission denied" on the Linux machine

**Symptom:** Running `bash quickstart.sh` errors on `systemctl enable sshd`.

**Cause:** quickstart.sh assumes you have sudo configured. On a minimal Arch install, the regular user may not be in the `wheel` group yet.

**Fix:** Run as root, or create the user in the `wheel` group during install (the setup agent's updated plans handle this automatically).

### Bundle .zip is missing the SSH public key

**Symptom:** `authorized_keys` setup in `quickstart.sh` can't find the `.pub` file.

**Cause:** You skipped the SSH step in the wizard.

**Fix:** Re-run the wizard, go back to the SSH step, generate a key, then re-export.
