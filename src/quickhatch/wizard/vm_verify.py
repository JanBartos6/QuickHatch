"""VM verification: boot the generated install.sh inside a throwaway VM.

Gives offline single-machine users a safety net: before they wipe their
real PC, run the LLM-generated install script against a live-ISO VM and
capture exit codes + logs. If it fails, the errors flow back to the
agent for a rewrite round.

Two backends are supported, in preference order:

1. **VirtualBox** (`VBoxManage`) — most common on Windows.
2. **QEMU** (`qemu-system-x86_64`) — fallback; works cross-platform.

We stay deliberately thin: shell out, don't import any VM library. The
module is fully unit-testable by monkeypatching `subprocess.run` because
every external call funnels through `_run()`.

The installer is NOT guaranteed to fully succeed inside a VM — some
steps (GPU drivers, Wi-Fi firmware, TPM) are hardware-specific. The
agent is told this via a VM-aware prompt block so it marks those steps
as "skipped in VM" rather than failing hard. What we DO verify:

- The script parses (bash -n passes).
- Partitioning and base install reach completion.
- Package installs resolve against mirrors.
- First-boot oneshot service is wired up correctly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Types & helpers
# ---------------------------------------------------------------------------


@dataclass
class VerifyResult:
    """Outcome of a single VM-verification run."""

    success: bool
    backend: str  # "virtualbox" | "qemu" | ""
    exit_code: int | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    stages: list[str] = field(default_factory=list)
    error: str = ""
    vm_name: str = ""

    def summary(self) -> str:
        """One-line human-readable summary."""
        if self.success:
            n = len(self.stages)
            return f"VM verify passed ({self.backend}, {n} stage{'s' if n != 1 else ''})"
        if self.error:
            return f"VM verify failed ({self.backend or 'no backend'}): {self.error}"
        return f"VM verify failed ({self.backend}, exit={self.exit_code})"


ProgressCallback = Callable[[str], None]


def _noop_progress(msg: str) -> None:
    """Default no-op progress sink."""


def _run(
    cmd: list[str],
    *,
    timeout: int = 60,
    capture: bool = True,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """Single chokepoint for every subprocess call (easy to mock in tests)."""
    return subprocess.run(
        cmd,
        timeout=timeout,
        capture_output=capture,
        text=True,
        check=check,
    )


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------


def detect_backends() -> dict[str, str | None]:
    """Return absolute paths to VBoxManage / qemu binaries, or None if missing."""
    vbox = shutil.which("VBoxManage") or shutil.which("VBoxManage.exe")
    # On Windows, VirtualBox isn't always on PATH. The installer sets
    # VBOX_MSI_INSTALL_PATH (and sometimes VBOX_INSTALL_PATH) to the
    # actual install directory — check those first, then fall back to
    # the standard default locations. Users install VBox on arbitrary
    # drives (e.g. G:\VirtualBox\), so env-var lookup beats guessing.
    if not vbox:
        candidate_dirs: list[str] = []
        for env_var in ("VBOX_MSI_INSTALL_PATH", "VBOX_INSTALL_PATH"):
            v = os.environ.get(env_var, "").strip()
            if v:
                candidate_dirs.append(v)
        candidate_dirs.extend(
            [
                r"C:\Program Files\Oracle\VirtualBox",
                r"C:\Program Files (x86)\Oracle\VirtualBox",
            ]
        )
        # Registry (Windows only) — authoritative source of the install dir.
        try:  # pragma: no cover - Windows-only branch
            import winreg  # type: ignore[import-not-found]

            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(root, r"SOFTWARE\Oracle\VirtualBox") as key:
                        install_dir, _ = winreg.QueryValueEx(key, "InstallDir")
                        if install_dir:
                            candidate_dirs.append(install_dir)
                except OSError:
                    continue
        except ImportError:
            pass

        for d in candidate_dirs:
            guess = Path(d) / "VBoxManage.exe"
            if guess.exists():
                vbox = str(guess)
                break
    qemu = (
        shutil.which("qemu-system-x86_64")
        or shutil.which("qemu-system-x86_64.exe")
        or shutil.which("qemu-kvm")
    )
    return {"virtualbox": vbox, "qemu": qemu}


def pick_backend(prefer: str | None = None) -> str:
    """Return 'virtualbox', 'qemu', or '' if none available."""
    found = detect_backends()
    if prefer and found.get(prefer):
        return prefer
    if found["virtualbox"]:
        return "virtualbox"
    if found["qemu"]:
        return "qemu"
    return ""


# ---------------------------------------------------------------------------
# Pre-flight syntax check (cheap, runs even without a VM)
# ---------------------------------------------------------------------------


def bash_syntax_check(script_path: Path) -> tuple[bool, str]:
    """Run `bash -n` on the script if bash is available.

    Returns (ok, message). If bash isn't installed we count it as a pass
    rather than blocking — most Windows users won't have it, and the VM
    run is the real test anyway.
    """
    bash = shutil.which("bash") or shutil.which("bash.exe")
    if not bash:
        return True, "bash not available, syntax check skipped"
    try:
        proc = _run([bash, "-n", str(script_path)], timeout=30)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"syntax check error: {exc}"
    if proc.returncode == 0:
        return True, "bash -n passed"
    return False, (proc.stderr or proc.stdout or "syntax error").strip()[:500]


# ---------------------------------------------------------------------------
# VirtualBox backend
# ---------------------------------------------------------------------------


class VirtualBoxBackend:
    """VBoxManage wrapper — creates a disposable VM, runs install.sh, tears down."""

    def __init__(self, vbox_path: str, progress: ProgressCallback = _noop_progress):
        self.vbox = vbox_path
        self.progress = progress
        self.vm_name = f"quickhatch-verify-{uuid.uuid4().hex[:8]}"
        self.disk_path: Path | None = None
        self.tmpdir: Path | None = None

    def _vb(self, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
        return _run([self.vbox, *args], timeout=timeout)

    def create(self, iso_path: Path, ram_mb: int = 4096, disk_gb: int = 20) -> None:
        self.progress(f"Creating VM {self.vm_name}")
        self._vb("createvm", "--name", self.vm_name, "--ostype", "Linux_64", "--register")
        self._vb("modifyvm", self.vm_name, "--memory", str(ram_mb), "--cpus", "2")
        self._vb("modifyvm", self.vm_name, "--nic1", "nat")
        self._vb("modifyvm", self.vm_name, "--natpf1", "ssh,tcp,,22022,,22")
        self._vb("modifyvm", self.vm_name, "--firmware", "efi")

        # Disk
        self.tmpdir = Path(tempfile.gettempdir()) / self.vm_name
        self.tmpdir.mkdir(parents=True, exist_ok=True)
        self.disk_path = self.tmpdir / "disk.vdi"
        self._vb(
            "createmedium",
            "disk",
            "--filename",
            str(self.disk_path),
            "--size",
            str(disk_gb * 1024),
            "--format",
            "VDI",
        )
        self._vb("storagectl", self.vm_name, "--name", "SATA", "--add", "sata")
        self._vb(
            "storageattach",
            self.vm_name,
            "--storagectl",
            "SATA",
            "--port",
            "0",
            "--device",
            "0",
            "--type",
            "hdd",
            "--medium",
            str(self.disk_path),
        )
        self._vb(
            "storageattach",
            self.vm_name,
            "--storagectl",
            "SATA",
            "--port",
            "1",
            "--device",
            "0",
            "--type",
            "dvddrive",
            "--medium",
            str(iso_path),
        )

    def start(self, headless: bool = True) -> None:
        mode = "headless" if headless else "gui"
        self.progress(f"Starting VM {self.vm_name} ({mode})")
        self._vb("startvm", self.vm_name, "--type", mode)

    def type_command(self, command: str) -> None:
        """Type a shell command into the VM console and press Enter."""
        self.progress(f"Typing bootstrap command into {self.vm_name}")
        self._vb("controlvm", self.vm_name, "keyboardputstring", command, timeout=30)
        # Enter key press/release scancodes.
        self._vb("controlvm", self.vm_name, "keyboardputscancode", "1c", "9c", timeout=30)

    def boot_from_disk(self, headless: bool = True) -> None:
        """Power-cycle the VM with the ISO detached so the installed OS boots."""
        mode = "headless" if headless else "gui"
        self.progress(f"Powering off {self.vm_name} before installed-OS boot")
        try:
            self._vb("controlvm", self.vm_name, "poweroff", timeout=30)
        except Exception:
            pass
        time.sleep(2.0)
        self.progress(f"Detaching ISO and booting {self.vm_name} from disk")
        self._vb(
            "storageattach",
            self.vm_name,
            "--storagectl",
            "SATA",
            "--port",
            "1",
            "--device",
            "0",
            "--type",
            "dvddrive",
            "--medium",
            "none",
        )
        self._vb("startvm", self.vm_name, "--type", mode)

    def teardown(self) -> None:
        self.progress(f"Tearing down {self.vm_name}")
        try:
            self._vb("controlvm", self.vm_name, "poweroff", timeout=30)
        except Exception:
            pass
        time.sleep(1.0)
        try:
            self._vb("unregistervm", self.vm_name, "--delete", timeout=60)
        except Exception:
            pass
        # Remove the temp dir we created for the virtual disk. VBoxManage
        # already deleted disk.vdi via --delete, but the parent dir lingers.
        if self.tmpdir and self.tmpdir.exists():
            try:
                shutil.rmtree(self.tmpdir, ignore_errors=True)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# QEMU backend
# ---------------------------------------------------------------------------


class QemuBackend:
    """Minimal QEMU wrapper — one `qemu-system-x86_64` invocation."""

    def __init__(self, qemu_path: str, progress: ProgressCallback = _noop_progress):
        self.qemu = qemu_path
        self.progress = progress
        self.vm_name = f"quickhatch-verify-{uuid.uuid4().hex[:8]}"
        self.proc: subprocess.Popen | None = None
        self.disk_path: Path | None = None
        self.tmpdir: Path | None = None

    def create(self, iso_path: Path, ram_mb: int = 4096, disk_gb: int = 20) -> None:
        self.progress(f"Creating QEMU disk for {self.vm_name}")
        self.tmpdir = Path(tempfile.gettempdir()) / self.vm_name
        self.tmpdir.mkdir(parents=True, exist_ok=True)
        self.disk_path = self.tmpdir / "disk.qcow2"
        qemu_img = shutil.which("qemu-img") or shutil.which("qemu-img.exe") or "qemu-img"
        _run(
            [qemu_img, "create", "-f", "qcow2", str(self.disk_path), f"{disk_gb}G"],
            timeout=60,
        )
        self._iso = iso_path
        self._ram = ram_mb

    def start(self) -> None:
        assert self.disk_path is not None
        cmd = [
            self.qemu,
            "-name",
            self.vm_name,
            "-m",
            str(self._ram),
            "-smp",
            "2",
            "-drive",
            f"file={self.disk_path},format=qcow2,if=virtio",
            "-cdrom",
            str(self._iso),
            "-boot",
            "d",
            "-netdev",
            "user,id=n0,hostfwd=tcp::22022-:22",
            "-device",
            "virtio-net,netdev=n0",
            "-display",
            "none",
            "-daemonize",
        ]
        self.progress(f"Starting QEMU VM {self.vm_name}")
        self.proc = subprocess.Popen(cmd)  # noqa: S603

    def boot_from_disk(self, headless: bool = True) -> None:
        """Restart QEMU from the installed disk.

        The current QEMU launcher uses daemonized processes without a monitor,
        so reliable ISO detach/reboot support needs a separate backend pass.
        Fail closed instead of pretending post-reboot certification happened.
        """
        del headless
        raise RuntimeError("QEMU post-reboot certification is not implemented yet")

    def teardown(self) -> None:
        self.progress(f"Tearing down {self.vm_name}")
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        if self.tmpdir and self.tmpdir.exists():
            try:
                shutil.rmtree(self.tmpdir, ignore_errors=True)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# SSH into the live VM and run the script
# ---------------------------------------------------------------------------


def _vbox_vm_state(vm_name: str) -> str | None:
    """Return VMState from VBoxManage (running/paused/etc.) or None if unknown."""
    if not vm_name:
        return None
    paths = detect_backends()
    vbox = paths.get("virtualbox")
    if not vbox:
        return None
    try:
        proc = _run([vbox, "showvminfo", vm_name, "--machinereadable"], timeout=15)
    except Exception:
        return None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("VMState="):
            return line.split("=", 1)[1].strip().strip('"')
    return None


def _wait_for_vm_ssh(
    port: int,
    timeout: int = 300,
    progress: ProgressCallback = _noop_progress,
    vm_name: str = "",
) -> bool:
    """Poll until SSH answers on 127.0.0.1:port (the forwarded VM port).

    Every 60s, probe the VM's own state via VBoxManage — if it's paused
    (e.g. host-disk-full auto-pause), surface that as a clear progress
    message so the user isn't left guessing at a silent 'Connection
    refused' loop.
    """
    import socket

    deadline = time.monotonic() + timeout
    progress(f"Waiting for VM SSH on 127.0.0.1:{port}")
    last_state_check = 0.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
                banner = sock.recv(64)
                if banner.startswith(b"SSH-"):
                    progress("VM SSH is up")
                    return True
        except OSError:
            pass
        now = time.monotonic()
        if vm_name and now - last_state_check > 60:
            last_state_check = now
            state = _vbox_vm_state(vm_name)
            if state == "paused":
                progress(
                    f"⚠️  VM '{vm_name}' is PAUSED — check host disk space "
                    f"(VBox auto-pauses on host-side I/O errors). Free up space "
                    f"and run: VBoxManage controlvm {vm_name} resume"
                )
            elif state in ("poweroff", "aborted"):
                progress(f"⚠️  VM '{vm_name}' is {state} — cannot recover automatically.")
                return False
        time.sleep(5)
    return False


def run_script_in_vm(
    script_path: Path,
    ssh_user: str,
    ssh_port: int,
    ssh_key: str | None,
    progress: ProgressCallback = _noop_progress,
) -> tuple[int, str, str]:
    """scp the script into the live VM and run it. Returns (rc, stdout, stderr)."""
    ssh = shutil.which("ssh") or shutil.which("ssh.exe")
    scp = shutil.which("scp") or shutil.which("scp.exe")
    if not ssh or not scp:
        return (127, "", "ssh/scp not available on host")

    common = [
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
        "-P" if False else "-p",  # placeholder; overridden per-tool
    ]
    key_args = ["-i", ssh_key] if ssh_key else []

    progress("Copying install.sh into VM")
    scp_cmd = [
        scp,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-P",
        str(ssh_port),
        *key_args,
        str(script_path),
        f"{ssh_user}@127.0.0.1:/tmp/install.sh",
    ]
    cp = _run(scp_cmd, timeout=60)
    if cp.returncode != 0:
        return (cp.returncode, cp.stdout, cp.stderr)

    progress("Running install.sh inside VM (this can take a while)")
    ssh_cmd = [
        ssh,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-p",
        str(ssh_port),
        *key_args,
        f"{ssh_user}@127.0.0.1",
        "sudo QUICKHATCH_VM_VERIFY=1 bash -x /tmp/install.sh 2>&1 | tail -c 200000",
    ]
    _ = common  # silence unused
    rc = _run(ssh_cmd, timeout=1800)  # 30 min hard cap
    return (rc.returncode, rc.stdout, rc.stderr)


def run_command_in_vm(
    command: str,
    ssh_user: str,
    ssh_port: int,
    ssh_key: str | None,
    progress: ProgressCallback = _noop_progress,
    timeout: int = 300,
) -> tuple[int, str, str]:
    """Run one command over SSH inside the VM."""
    ssh = shutil.which("ssh") or shutil.which("ssh.exe")
    if not ssh:
        return (127, "", "ssh not available on host")

    key_args = ["-i", ssh_key] if ssh_key else []
    progress(f"Running VM audit command: {command[:120]}")
    ssh_cmd = [
        ssh,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
        "-p",
        str(ssh_port),
        *key_args,
        f"{ssh_user}@127.0.0.1",
        command,
    ]
    rc = _run(ssh_cmd, timeout=timeout)
    return (rc.returncode, rc.stdout, rc.stderr)


def _try_live_ssh_bootstrap(
    vm: VirtualBoxBackend | QemuBackend,
    command: str | None,
    *,
    ssh_port: int,
    progress: ProgressCallback,
    vm_name: str,
    wait_seconds: int,
) -> bool:
    """Try to make an Arch-style live shell SSH-ready without human input."""
    if not command:
        return False
    if not hasattr(vm, "type_command"):
        progress("Live SSH bootstrap skipped: backend cannot type into the VM console")
        return False
    progress(
        f"VM SSH is not up yet; trying live-console SSH bootstrap after {wait_seconds}s boot grace"
    )
    time.sleep(max(wait_seconds, 0))
    try:
        vm.type_command(command)  # type: ignore[attr-defined]
    except Exception as exc:
        progress(f"Live SSH bootstrap failed while typing command: {exc}")
        return False
    return _wait_for_vm_ssh(ssh_port, timeout=180, progress=progress, vm_name=vm_name)


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def verify_install_script(
    script_path: Path,
    iso_path: Path | None,
    ssh_user: str = "quickhatch",
    ssh_key: str | None = None,
    progress: ProgressCallback = _noop_progress,
    backend: str | None = None,
    post_reboot_user: str | None = None,
    post_reboot_commands: list[str] | None = None,
    require_post_reboot: bool = False,
    live_bootstrap_command: str | None = None,
    live_bootstrap_wait_seconds: int = 45,
) -> VerifyResult:
    """End-to-end: syntax check → spin VM → boot ISO → SSH in → run script.

    `iso_path` is the live-USB ISO the user already downloaded. If None,
    we only do the syntax check and return early — still useful as a
    cheap sanity signal.
    """
    result = VerifyResult(success=False, backend="")
    result.vm_name = ""
    result.stages.append("syntax-check")

    ok, msg = bash_syntax_check(script_path)
    progress(f"Syntax check: {msg}")
    if not ok:
        result.error = f"bash -n failed: {msg}"
        return result

    if iso_path is None:
        # No ISO provided → syntax check only. Mark as partial pass.
        result.success = True
        result.backend = "syntax-only"
        return result

    picked = backend or pick_backend()
    if not picked:
        result.error = "No VM backend found (install VirtualBox or QEMU)"
        return result
    result.backend = picked
    result.stages.append(f"backend:{picked}")

    paths = detect_backends()
    if picked == "virtualbox":
        vm = VirtualBoxBackend(paths["virtualbox"] or "VBoxManage", progress=progress)
    else:
        vm = QemuBackend(paths["qemu"] or "qemu-system-x86_64", progress=progress)
    result.vm_name = vm.vm_name

    try:
        vm.create(iso_path)
        result.stages.append("vm-created")
        vm.start()
        result.stages.append("vm-started")

        initial_ssh_timeout = 60 if live_bootstrap_command else 600
        ssh_ready = _wait_for_vm_ssh(
            22022,
            timeout=initial_ssh_timeout,
            progress=progress,
            vm_name=vm.vm_name,
        )
        if not ssh_ready:
            ssh_ready = _try_live_ssh_bootstrap(
                vm,
                live_bootstrap_command,
                ssh_port=22022,
                progress=progress,
                vm_name=vm.vm_name,
                wait_seconds=live_bootstrap_wait_seconds,
            )
        if not ssh_ready:
            result.error = "VM never answered on SSH (boot or network failed)"
            return result
        result.stages.append("ssh-up")

        rc, out, err = run_script_in_vm(script_path, ssh_user, 22022, ssh_key, progress=progress)
        result.exit_code = rc
        result.stdout_tail = (out or "")[-4000:]
        result.stderr_tail = (err or "")[-4000:]
        result.stages.append(f"script-exit:{rc}")

        # Interpret: 0 = pass. 100-series codes reserved for "skipped in VM"
        # (set by install.sh when it detects QUICKHATCH_VM_VERIFY=1 on a
        # hardware-only step). Treat those as soft-pass.
        result.success = rc == 0 or (100 <= (rc or 0) < 110)
        if not result.success and not result.error:
            raw = (result.stderr_tail or result.stdout_tail or "").strip()
            # Recognise the common "vanilla live ISO" failure — SSH
            # answered but authentication failed because the live user
            # wasn't pre-seeded with our key. Give an actionable hint
            # instead of the raw scp stderr.
            if "Permission denied" in raw and ("publickey" in raw or "password" in raw):
                result.error = (
                    f"SSH authentication failed for user '{ssh_user}'. VM verify "
                    f"needs a customized ISO with that user pre-created and our SSH "
                    f"public key seeded in its ~/.ssh/authorized_keys (plus "
                    f"passwordless sudo). Vanilla distribution ISOs won't work as-is."
                )
            else:
                result.error = raw[:300]

        if result.success and (require_post_reboot or post_reboot_commands):
            result.stages.append("post-reboot-requested")
            try:
                vm.boot_from_disk()
            except Exception as exc:
                result.success = False
                result.error = f"Could not boot installed VM from disk: {exc}"
                return result

            if not _wait_for_vm_ssh(22022, timeout=600, progress=progress, vm_name=vm.vm_name):
                result.success = False
                result.error = "Installed VM never answered on SSH after ISO detach/reboot"
                return result
            result.stages.append("post-reboot-ssh-up")

            audit_user = post_reboot_user or ssh_user
            for idx, command in enumerate(post_reboot_commands or [], start=1):
                rc, out, err = run_command_in_vm(
                    command,
                    audit_user,
                    22022,
                    ssh_key,
                    progress=progress,
                    timeout=300,
                )
                result.stages.append(f"post-reboot-audit:{idx}:{rc}")
                result.stdout_tail = ((result.stdout_tail or "") + "\n" + (out or ""))[-4000:]
                result.stderr_tail = ((result.stderr_tail or "") + "\n" + (err or ""))[-4000:]
                if rc != 0:
                    result.success = False
                    raw = (err or out or "").strip()
                    result.error = (
                        f"Post-reboot audit command failed ({rc}): {command}; {raw[:300]}"
                    )
                    return result
    except subprocess.TimeoutExpired:
        result.error = "VM operation timed out"
    except Exception as exc:  # pragma: no cover - defensive
        result.error = f"VM verify exception: {exc}"
    finally:
        try:
            vm.teardown()
            result.stages.append("vm-torn-down")
        except Exception:
            pass

    return result


# ---------------------------------------------------------------------------
# Prompt glue: tell the agent when it's writing a script for VM-verify
# ---------------------------------------------------------------------------


VM_AWARE_PROMPT_BLOCK = """\
== VM-VERIFY MODE ==
The script you produce WILL be executed inside a throwaway VirtualBox/QEMU VM
first, before the user runs it on real hardware.

This means:
- Hardware-specific steps (proprietary GPU drivers, Wi-Fi firmware, TPM,
  Secure Boot shim signing, fingerprint readers) WILL NOT work inside the VM.
  For each such step, gate it with:
      if [ "${QUICKHATCH_VM_VERIFY:-0}" = "1" ]; then
          echo "[vm] skipping <step-name> — hardware not present"
          exit_code=105   # 100-109 = soft-pass, VM-verify treats as success
      else
          <real command>
      fi
- Destructive disk operations (wipefs, parted, mkfs) ARE safe in the VM —
  the VM has a disposable virtual disk. Still honour the disk-confirm prompt,
  but auto-accept it when QUICKHATCH_VM_VERIFY=1 by reading from a
  pre-seeded /tmp/quickhatch-disk.txt if that file exists.
- The VM has one NIC behind NAT. DNS and package mirrors work. No Wi-Fi.
- The VM's only disk is typically /dev/sda or /dev/vda. The script should
  auto-detect — don't hard-code /dev/nvme0n1 unless the user profile
  explicitly specifies it.
- Do not write a VM-only happy path that assumes Arch/KDE/NVIDIA or replays
  a previous successful run. Use the VM's actual distro/package manager and
  keep the same generic decisions you would use on real hardware, with only
  hardware-specific pieces guarded or skipped.

Structure every potentially hardware-only command with the guard above so
the VM run can complete end-to-end and report exit 0.
"""


def vm_aware_prompt_block(enabled: bool) -> str:
    """Return the VM-aware prompt block only when verification is requested."""
    return VM_AWARE_PROMPT_BLOCK if enabled else ""


# ---------------------------------------------------------------------------
# User-in-the-loop assisted verify
# ---------------------------------------------------------------------------


def build_key_paste_snippet(public_key: str) -> str:
    """Return a one-line snippet the user pastes into the live VM terminal.

    Works on Arch / Debian / Ubuntu / Fedora / openSUSE / Alpine live ISOs
    that boot into a root shell with sshd already running (all the
    mainstream ones do). Installs the public key into /root/.ssh, makes
    sure sshd is up, and prints READY when done so the user knows to
    come back to QuickHatch.
    """
    # Strip newlines — authorized_keys is one-line-per-key. Escape any
    # single quotes so a key with a comment like
    #   ssh-ed25519 AAAA... user's laptop
    # doesn't break the surrounding shell echo.
    pk = public_key.replace("\n", "").replace("\r", "").strip()
    pk = pk.replace("'", "'\\''")  # close-quote, escape, reopen-quote
    return (
        f"mkdir -p /root/.ssh && "
        f"echo '{pk}' >> /root/.ssh/authorized_keys && "
        f"chmod 700 /root/.ssh && chmod 600 /root/.ssh/authorized_keys && "
        f"(systemctl start sshd 2>/dev/null || rc-service sshd start 2>/dev/null || "
        f"service ssh start 2>/dev/null || true) && "
        f"echo READY"
    )


@dataclass
class AssistedSession:
    """Live state for a user-in-the-loop VM verify run.

    Phase A: boot — VM is created, started in GUI mode with an ISO. The
    user sees the VM window, runs `paste_snippet` in its terminal to
    install our public key into /root/.ssh.
    Phase B: finish — once the user clicks Ready, we SSH in as root,
    scp the install script, run it, capture the result, tear down.
    """

    vm_name: str
    backend: str
    iso_path: Path
    script_path: Path
    public_key: str
    paste_snippet: str
    ssh_user: str = "root"
    ssh_port: int = 22022
    ssh_key: str | None = None
    # The backend instance — kept alive so we can tear it down later.
    _vm: VirtualBoxBackend | QemuBackend | None = None


def start_assisted_session(
    script_path: Path,
    iso_path: Path,
    public_key: str,
    ssh_key: str | None = None,
    backend: str | None = None,
    progress: ProgressCallback = _noop_progress,
) -> tuple[AssistedSession | None, str]:
    """Phase A: create the VM, attach the ISO, boot it in GUI mode.

    Returns ``(session, error)``. On success ``session`` is non-None and
    ``error`` is empty. On failure ``session`` is None and ``error``
    explains what went wrong.
    """
    if not iso_path.exists():
        return None, f"ISO not found: {iso_path}"

    ok, msg = bash_syntax_check(script_path)
    progress(f"Syntax check: {msg}")
    if not ok:
        return None, f"bash -n failed: {msg}"

    picked = backend or pick_backend()
    if not picked:
        return None, "No VM backend found (install VirtualBox or QEMU)"

    paths = detect_backends()
    if picked == "virtualbox":
        vm: VirtualBoxBackend | QemuBackend = VirtualBoxBackend(
            paths["virtualbox"] or "VBoxManage", progress=progress
        )
    else:
        vm = QemuBackend(paths["qemu"] or "qemu-system-x86_64", progress=progress)

    try:
        vm.create(iso_path)
        vm.start(headless=False)  # GUI mode so the user can interact
    except Exception as exc:
        try:
            vm.teardown()
        except Exception:
            pass
        return None, f"VM could not start: {exc}"

    snippet = build_key_paste_snippet(public_key)
    session = AssistedSession(
        vm_name=vm.vm_name,
        backend=picked,
        iso_path=iso_path,
        script_path=script_path,
        public_key=public_key,
        paste_snippet=snippet,
        ssh_key=ssh_key,
    )
    session._vm = vm
    return session, ""


def finish_assisted_session(
    session: AssistedSession,
    progress: ProgressCallback = _noop_progress,
    ssh_timeout: int = 60,
) -> VerifyResult:
    """Phase B: user has pasted the key. SSH in, run the script, tear down.

    ``ssh_timeout`` is short by default — by this point the VM has been
    up for however long the user took to paste the key, so SSH should
    answer within a few seconds. If it doesn't, the user either hasn't
    pasted yet or the key paste failed.
    """
    result = VerifyResult(success=False, backend=session.backend, vm_name=session.vm_name)
    result.stages = ["assisted-boot", f"backend:{session.backend}"]
    try:
        if not _wait_for_vm_ssh(
            session.ssh_port,
            timeout=ssh_timeout,
            progress=progress,
            vm_name=session.vm_name,
        ):
            result.error = (
                "SSH didn't answer — did you paste the snippet into the VM's "
                "root shell and press Enter? Try again."
            )
            return result
        result.stages.append("ssh-up")

        rc, out, err = run_script_in_vm(
            session.script_path,
            session.ssh_user,
            session.ssh_port,
            session.ssh_key,
            progress=progress,
        )
        result.exit_code = rc
        result.stdout_tail = (out or "")[-4000:]
        result.stderr_tail = (err or "")[-4000:]
        result.stages.append(f"script-exit:{rc}")
        result.success = rc == 0 or (100 <= (rc or 0) < 110)
        if not result.success and not result.error:
            result.error = (result.stderr_tail or result.stdout_tail or "").strip()[:300]
    except Exception as exc:  # pragma: no cover - defensive
        result.error = f"VM verify exception: {exc}"
    finally:
        if session._vm is not None:
            try:
                session._vm.teardown()
                result.stages.append("vm-torn-down")
            except Exception:
                pass
    return result
