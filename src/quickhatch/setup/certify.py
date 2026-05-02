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


def run_vm_certification(
    *,
    family: str,
    iso: Path | str,
    profile: Path | str,
    backend: str = "virtualbox",
    root: Path | str = ".quickhatch/certification",
    dry_run: bool = False,
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
    status = "blocked" if blockers else ("dry_run" if dry_run else "ready")
    success = not blockers and dry_run
    summary = {
        "family": normalized_family,
        "backend": backend,
        "iso": str(iso_path),
        "profile": str(profile_path),
        "status": status,
        "dry_run": dry_run,
        "snapshots": CERTIFICATION_SNAPSHOTS,
        "adapter": {"family": spine.family, "version": spine.adapter_version},
        "install_spine_command_count": len(spine.commands),
        "install_spine_verification_count": len(spine.verification),
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
            if success
            else "Certification runner prepared artifacts; live VM execution is not started by tests."
        ),
        snapshots=list(CERTIFICATION_SNAPSHOTS),
    )
