"""VM certification artifact runner.

This module provides the fast feedback contract for certification runs. It is
intentionally artifact-first: actual VM backends can evolve, while tests and CI
can validate family/profile/snapshot wiring without mutating a real VM.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from quickhatch.config import UserProfile
from quickhatch.wizard.vm_verify import (
    VerifyResult,
    build_key_paste_snippet,
    verify_install_script,
)

from .capabilities import CapabilityStore, dangerous_command_requirements
from .families import get_family_installer
from .preflight import preflight_setup_command
from .types import CertificationResult, SetupFacts

CERTIFIED_FAMILIES = {"arch", "ubuntu", "debian", "fedora"}
CERTIFICATION_SNAPSHOTS = [
    "live_booted",
    "base_installed",
    "post_reboot",
    "desktop_installed",
]


def _timestamp() -> str:
    millis = int((time.time() % 1) * 1000)
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{millis:03d}-{uuid.uuid4().hex[:6]}"


def _shell_comment(text: str) -> str:
    return "\n".join(f"# {line}" if line else "#" for line in str(text).splitlines())


def build_certification_script(
    family: str,
    commands: list[str],
    verification: list[str],
    *,
    target_disk: str = "/dev/sda",
) -> str:
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        _shell_comment(f"QuickHatch VM certification install spine for {family}"),
        "export QUICKHATCH_VM_CERTIFY=1",
        f"export QUICKHATCH_TARGET_DISK=${{QUICKHATCH_TARGET_DISK:-{shlex.quote(target_disk)}}}",
        "",
    ]
    for idx, command in enumerate(commands, start=1):
        lines.extend(
            [
                f"echo '[quickhatch-certify] command {idx}/{len(commands)}'",
                command,
                "",
            ]
        )
    if verification:
        lines.append("echo '[quickhatch-certify] verification'")
    for idx, command in enumerate(verification, start=1):
        lines.extend(
            [
                f"echo '[quickhatch-certify] verification {idx}/{len(verification)}'",
                command,
                "",
            ]
        )
    lines.extend(
        [
            "echo '[quickhatch-certify] syncing installed filesystem'",
            "sync",
            "",
        ]
    )
    lines.append("echo '[quickhatch-certify] completed'")
    return "\n".join(lines).rstrip() + "\n"


def _write_vm_result(artifact_dir: Path, result: VerifyResult) -> None:
    payload = {
        "success": result.success,
        "backend": result.backend,
        "exit_code": result.exit_code,
        "stdout_tail": result.stdout_tail,
        "stderr_tail": result.stderr_tail,
        "stages": result.stages,
        "error": result.error,
        "vm_name": result.vm_name,
        "summary": result.summary(),
    }
    (artifact_dir / "vm-result.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _append_event(artifact_dir: Path, event_type: str, **payload: object) -> None:
    event = {
        "event_id": payload.pop("event_id", f"cert-{uuid.uuid4().hex[:12]}"),
        "timestamp": time.time(),
        "event_type": event_type,
        **payload,
    }
    with (artifact_dir / "certification-run.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True, default=str) + "\n")


def _preflight_certification_spine(
    *,
    artifact_dir: Path,
    commands: list[str],
    facts: SetupFacts,
    install_profile: str,
    target_disk: str,
) -> list[str]:
    """Run the VM install spine through the same safety gate used by setup runs.

    Certification uses deterministic family adapters, so it may create harness
    grants instead of user grants, but it must still surface every dangerous
    command as a capability-backed preflight event.
    """

    blockers: list[str] = []
    disk_inventory = f"NAME SIZE TYPE MOUNTPOINTS\n{target_disk} unknown disk\n"
    store = CapabilityStore(
        run_id=f"certification-{artifact_dir.name}",
        install_profile=install_profile or "full_disk_wipe",
    )
    for command_index, command in enumerate(commands, start=1):
        requirements = dangerous_command_requirements(command)
        capability_ids: list[str] = []
        for requirement_index, requirement in enumerate(requirements, start=1):
            preview_id = f"cert-preview-{command_index}-{requirement_index}"
            grant = store.grant(
                kind=requirement["kind"],
                target=requirement.get("target", ""),
                confirmation_text=("VM certification deterministic install spine dry-run preview."),
                confirmation_id=f"VM-CERT {target_disk}",
                reason=requirement.get("reason", "VM certification install spine"),
                source="harness_generated",
                one_time_use=False,
                disk_inventory=disk_inventory,
                preview_id=preview_id,
            )
            capability_ids.append(grant.id)
            _append_event(
                artifact_dir,
                "capability_granted",
                stage="certification",
                command_index=command_index,
                command=command,
                requirement=requirement,
                capability_id=grant.id,
                capability_kind=grant.kind,
                capability_target=grant.target,
                dry_run_preview_id=preview_id,
                confirmation_id=grant.confirmation_id,
                one_time_use=grant.one_time_use,
                source=grant.source,
            )
        error = preflight_setup_command(
            command,
            facts,
            capability_store=store,
            disk_inventory=disk_inventory,
        )
        _append_event(
            artifact_dir,
            "certification_preflight_checked",
            stage="certification",
            command_index=command_index,
            command=command,
            parsed_requirements=requirements,
            capability_ids=capability_ids,
            blocked=bool(error),
            error=error or "",
        )
        if error:
            blockers.append(f"Certification preflight blocked command {command_index}: {error}")
    return blockers


def _public_key_for_private_key(private_key: Path) -> tuple[str, str]:
    public_key_path = private_key.with_name(private_key.name + ".pub")
    if public_key_path.exists():
        return public_key_path.read_text(encoding="utf-8").strip(), ""

    ssh_keygen = shutil.which("ssh-keygen") or shutil.which("ssh-keygen.exe")
    if not ssh_keygen:
        return "", "ssh-keygen not found; cannot derive public key for VM certification"
    proc = subprocess.run(  # noqa: S603
        [ssh_keygen, "-y", "-f", str(private_key)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return "", (proc.stderr or proc.stdout or "ssh-keygen failed").strip()
    return proc.stdout.strip(), ""


def _prepare_certification_key(artifact_dir: Path, ssh_key: str | None) -> tuple[str, str, str]:
    if ssh_key:
        private_key = Path(ssh_key)
        if not private_key.exists():
            return "", "", f"SSH key not found: {private_key}"
        public_key, error = _public_key_for_private_key(private_key)
        return str(private_key), public_key, error

    ssh_keygen = shutil.which("ssh-keygen") or shutil.which("ssh-keygen.exe")
    if not ssh_keygen:
        return "", "", "ssh-keygen not found; cannot generate VM certification key"

    private_key = artifact_dir / "quickhatch-certify-ed25519"
    proc = subprocess.run(  # noqa: S603
        [
            ssh_keygen,
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "quickhatch-certify",
            "-f",
            str(private_key),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return "", "", (proc.stderr or proc.stdout or "ssh-keygen failed").strip()
    public_key = private_key.with_name(private_key.name + ".pub").read_text(encoding="utf-8")
    return str(private_key), public_key.strip(), ""


def run_vm_certification(
    *,
    family: str,
    iso: Path | str,
    profile: Path | str,
    backend: str = "virtualbox",
    root: Path | str = ".quickhatch/certification",
    dry_run: bool = False,
    ssh_user: str = "root",
    ssh_key: str | None = None,
    post_reboot_user: str | None = None,
    require_post_reboot: bool = True,
    target_disk: str | None = None,
) -> CertificationResult:
    normalized_family = (family or "").lower()
    effective_live_ssh_user = (
        "user" if ssh_user == "root" and normalized_family == "debian" else ssh_user
    )
    artifact_dir = Path(root) / normalized_family / _timestamp()
    artifact_dir.mkdir(parents=True, exist_ok=True)

    blockers: list[str] = []
    iso_path = Path(iso)
    profile_path = Path(profile)
    if normalized_family not in CERTIFIED_FAMILIES:
        blockers.append(f"Unsupported certification family: {family}")
    if not iso_path.exists():
        blockers.append(f"ISO not found: {iso_path}")
    if not profile_path.exists():
        blockers.append(f"Profile not found: {profile_path}")

    loaded_profile = UserProfile()
    if profile_path.exists():
        try:
            loaded_profile = (
                UserProfile.from_dict(json.loads(profile_path.read_text(encoding="utf-8")))
                or UserProfile()
            )
        except Exception as exc:
            blockers.append(f"Could not load profile JSON: {exc}")
    adapter = get_family_installer(normalized_family)
    if post_reboot_user:
        effective_post_reboot_user = post_reboot_user
    elif hasattr(adapter, "target_user"):
        effective_post_reboot_user = adapter.target_user(loaded_profile)  # type: ignore[attr-defined]
    else:
        effective_post_reboot_user = (
            getattr(loaded_profile, "linux_user", "") or ""
        ).strip() or "quickhatch"
    facts = SetupFacts(
        distro_family=normalized_family,
        is_live=True,
        is_root=True,
        current_user="root",
        login_user="root",
        sudo_nopasswd=True,
    )
    spine = adapter.build_install_spine(facts, loaded_profile)
    post_reboot_audit = adapter.final_boot_audit(SetupFacts(distro_family=normalized_family))
    effective_target_disk = target_disk or ("/dev/vda" if backend == "qemu" else "/dev/sda")
    script = build_certification_script(
        normalized_family,
        spine.commands,
        spine.verification,
        target_disk=effective_target_disk,
    )
    script_path = artifact_dir / "certify-install.sh"
    script_path.write_text(script, encoding="utf-8", newline="\n")
    (artifact_dir / "final-facts.json").write_text(
        json.dumps(asdict(facts), indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    _append_event(
        artifact_dir,
        "certification_planned",
        family=normalized_family,
        backend=backend,
        dry_run=dry_run,
        target_disk=effective_target_disk,
        adapter=spine.family,
        adapter_version=spine.adapter_version,
        command_count=len(spine.commands),
        verification_count=len(spine.verification),
        post_reboot_audit_count=len(post_reboot_audit),
    )
    if not blockers:
        blockers.extend(
            _preflight_certification_spine(
                artifact_dir=artifact_dir,
                commands=spine.commands,
                facts=facts,
                install_profile=(
                    getattr(loaded_profile, "install_profile", "") or "full_disk_wipe"
                ),
                target_disk=effective_target_disk,
            )
        )

    vm_result: VerifyResult | None = None
    effective_ssh_key = ssh_key
    live_bootstrap_command = None
    generated_key = False
    if not blockers and not dry_run:
        effective_ssh_key, public_key, key_error = _prepare_certification_key(
            artifact_dir,
            ssh_key,
        )
        generated_key = ssh_key is None and bool(effective_ssh_key)
        if key_error:
            blockers.append(key_error)
        elif public_key:
            live_bootstrap_command = build_key_paste_snippet(public_key)

    if not blockers and not dry_run:
        progress_log = artifact_dir / "progress.log"
        graphical_live_bootstrap_users = {
            "debian": "user",
            "fedora": "liveuser",
            "ubuntu": "ubuntu",
        }
        graphical_live_bootstrap_user = graphical_live_bootstrap_users.get(
            normalized_family,
            "",
        )
        live_bootstrap_wait_seconds = 120 if graphical_live_bootstrap_user else 45

        def _progress(message: str) -> None:
            with progress_log.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")

        vm_result = verify_install_script(
            script_path,
            iso_path=iso_path,
            ssh_user=effective_live_ssh_user,
            ssh_key=effective_ssh_key,
            progress=_progress,
            backend=backend,
            post_reboot_user=effective_post_reboot_user,
            post_reboot_commands=post_reboot_audit,
            require_post_reboot=require_post_reboot,
            live_bootstrap_command=live_bootstrap_command,
            live_bootstrap_user=graphical_live_bootstrap_user,
            live_bootstrap_wait_seconds=live_bootstrap_wait_seconds,
        )
        _write_vm_result(artifact_dir, vm_result)
        _append_event(
            artifact_dir,
            "vm_certification_finished",
            success=vm_result.success,
            backend=vm_result.backend,
            exit_code=vm_result.exit_code,
            stages=vm_result.stages,
            error=vm_result.error,
            stdout_tail=vm_result.stdout_tail,
            stderr_tail=vm_result.stderr_tail,
            summary=vm_result.summary(),
            vm_name=vm_result.vm_name,
        )
        if not vm_result.success:
            blockers.append(vm_result.error or vm_result.summary())

    status = "blocked" if blockers else ("dry_run" if dry_run else "passed")
    success = not blockers and (dry_run or bool(vm_result and vm_result.success))
    if blockers and vm_result is None:
        _append_event(
            artifact_dir,
            "certification_blocked",
            success=False,
            status=status,
            blockers=blockers,
        )
    elif dry_run:
        _append_event(
            artifact_dir,
            "certification_finished",
            success=success,
            status=status,
            blockers=blockers,
        )
    summary = {
        "family": normalized_family,
        "backend": backend,
        "iso": str(iso_path),
        "profile": str(profile_path),
        "status": status,
        "dry_run": dry_run,
        "snapshots": CERTIFICATION_SNAPSHOTS,
        "snapshot_status": "declared_only_until_backend_snapshot_hooks_land",
        "adapter": {"family": spine.family, "version": spine.adapter_version},
        "install_spine_command_count": len(spine.commands),
        "install_spine_verification_count": len(spine.verification),
        "certification_script": str(script_path),
        "runlog": str(artifact_dir / "certification-run.jsonl"),
        "final_facts": str(artifact_dir / "final-facts.json"),
        "ssh_user": effective_live_ssh_user,
        "ssh_key": str(effective_ssh_key) if effective_ssh_key else "",
        "generated_ssh_key": generated_key,
        "target_disk": effective_target_disk,
        "post_reboot_user": effective_post_reboot_user,
        "require_post_reboot": require_post_reboot,
        "manual_intervention_required": False,
        "screenshots": [],
        "vm_result": asdict(vm_result) if vm_result else None,
        "blockers": blockers,
    }
    (artifact_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (artifact_dir / "adapter-spine.json").write_text(
        json.dumps(asdict(spine), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return CertificationResult(
        success=success,
        family=normalized_family,
        status=status,
        artifact_dir=str(artifact_dir),
        blockers=blockers,
        summary=(
            "Certification dry-run artifact written."
            if dry_run and success
            else (
                "VM certification passed."
                if success
                else "VM certification failed; see artifacts for the VM/backend error."
            )
        ),
        snapshots=list(CERTIFICATION_SNAPSHOTS),
    )
