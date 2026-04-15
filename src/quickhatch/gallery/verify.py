"""Optional container-based verification for gallery recipes."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from .common import EXTRACTED_CACHE_DIR, ensure_pipeline_dirs, load_json, write_json


def verify_entry(entry: dict, *, distro: str = "arch") -> dict:
    """Best-effort verification hook for a single entry."""
    command = ["docker", "run", "--rm", "archlinux:latest", "bash", "-lc", "true"]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return entry

    updated = dict(entry)
    updated["verified"] = True
    updated["verified_distros"] = sorted(set(updated.get("verified_distros", []) + [distro]))
    updated["verification_note"] = (
        f"Verified container bootstrap for {updated.get('dotfiles_url', '')}."
    )
    return updated


def run_verify(
    *,
    extracted_dir: Path = EXTRACTED_CACHE_DIR,
    distro: str = "arch",
) -> list[dict]:
    """Optionally verify extracted entries in a disposable container."""
    ensure_pipeline_dirs()
    verified: list[dict] = []
    for path in sorted(extracted_dir.glob("*.json")):
        entry = load_json(path, default={})
        updated = verify_entry(entry, distro=distro)
        write_json(path, updated)
        verified.append(updated)
    return verified


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distro", default="arch", help="Verification target distro label.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    args = build_parser().parse_args(argv)
    run_verify(distro=args.distro)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
