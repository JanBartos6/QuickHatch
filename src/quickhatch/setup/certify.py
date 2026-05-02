"""VM certification artifact runner.

This module provides the fast feedback contract for certification runs. It is
intentionally artifact-first: actual VM backends can evolve, while tests and CI
can validate family/profile/snapshot wiring without mutating a real VM.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from quickhatch.config import UserProfile
from quickhatch.wizard.vm_verify import VerifyResult, verify_install_script

from .families import get_family_installer
from .types import CertificationResult, SetupFacts

CERTIFIED_FAMILIES = {"arch", "ubuntu", "debian", "fedora"}
CERTIFICATION_SNAPSHOTS = [
    "live_booted",
    "base_installed",
    "post_reboot",
    "desktop_installed",
]


def _timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _shell_comment(text: str) -> str:
    return "\n".join(f"# {line}" if line else "#" for line in str(text).splitlines())


def build_certification_script(family: str, commands: list[str], verification: list[str]) -> str:
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        _shell_comment(f"QuickHatch VM certification install spine for {family}"),
        "export QUICKHATCH_VM_CERTIFY=1",
        "export QUICKHATCH_TARGET_DISK=${QUICKHATCH_TARGET_DISK:-/dev/sda}",
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
        "timestamp": time.time(),
        "event_type": event_type,
        **payload,
    }
    with (artifact_dir / "certification-run.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True, default=str) + "\n")


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
    post_reboot_user: str = "jan",
    require_post_reboot: bool = True,
) -> CertificationResult:
    normalized_family = (family or "").lower()
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
    facts = SetupFacts(distro_family=normalized_family, is_live=True)
    spine = adapter.build_install_spine(facts, loaded_profile)
    post_reboot_audit = adapter.final_boot_audit(SetupFacts(distro_family=normalized_family))
    script = build_certification_script(normalized_family, spine.commands, spine.verification)
    script_path = artifact_dir / "certify-install.sh"
    script_path.write_text(script, encoding="utf-8")
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
        adapter=spine.family,
        adapter_version=spine.adapter_version,
        command_count=len(spine.commands),
        verification_count=len(spine.verification),
        post_reboot_audit_count=len(post_reboot_audit),
    )

    vm_result: VerifyResult | None = None
    if not blockers and not dry_run:
        progress_log = artifact_dir / "progress.log"

        def _progress(message: str) -> None:
            with progress_log.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")

        vm_result = verify_install_script(
            script_path,
            iso_path=iso_path,
            ssh_user=ssh_user,
            ssh_key=ssh_key,
            progress=_progress,
            backend=backend,
            post_reboot_user=post_reboot_user,
            post_reboot_commands=post_reboot_audit,
            require_post_reboot=require_post_reboot,
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
        )
        if not vm_result.success:
            blockers.append(vm_result.error or vm_result.summary())

    status = "blocked" if blockers else ("dry_run" if dry_run else "passed")
    success = not blockers and (dry_run or bool(vm_result and vm_result.success))
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
        "ssh_user": ssh_user,
        "post_reboot_user": post_reboot_user,
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
