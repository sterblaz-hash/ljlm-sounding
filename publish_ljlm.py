"""Safely generate and publish LJLM observation products in GitHub Actions.

Run only in an ephemeral GitHub Actions checkout on refs/heads/main.
Never run this on a local development working tree: it hard-resets the checkout.
On a non-fast-forward push race, rebuild *all* products from the new main;
never rebase divergent generated JSON/PNG artifacts.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable


MAX_ATTEMPTS = 3
GIT_TARGET = "main"
GENERATED_DIRS = (
    "climatology", "diagnostics", "timeseries", "trajectories", "verification",
)
CLEAN_DIRS = (*GENERATED_DIRS, "data")
PIPELINE = (
    ("Extract LJLM observations", [sys.executable, "extract_ljlm.py"]),
    ("Render LJLM soundings", [sys.executable, "render_ljlm.py", "--input", "data/latest.json"]),
    ("Render OBS and ICON-D2 overlay", [sys.executable, "render_icon_d2_verification.py"]),
    ("Render three-way model verification", [sys.executable, "render_model_verification.py"]),
    ("Update model verification statistics", [sys.executable, "build_model_verification_data.py", "--sync-recent-hours", "72"]),
    ("Update trajectories", [sys.executable, "build_trajectory_data.py", "--sync-recent-hours", "72"]),
    ("Update dashboard data", [sys.executable, "build_dashboard_data.py", "--sync-recent-hours", "72"]),
)


def run(args: list[str], *, allow_failure: bool = False) -> int:
    print("+", shlex.join(args), flush=True)
    completed = subprocess.run(args, check=False)
    if completed.returncode and not allow_failure:
        raise subprocess.CalledProcessError(completed.returncode, args)
    return completed.returncode


def stage_products(runner: Callable[..., int]) -> None:
    """Match the observation workflow's existing, explicit staging scope."""
    existing = [folder for folder in GENERATED_DIRS if Path(folder).is_dir()]
    if existing:
        runner(["git", "add", "-A", "--", *existing])

    data = Path("data")
    if data.is_dir():
        # Some observation JSONs are ignored by repository rules. Preserve the
        # previous workflow's `git add -f` semantics; do not add other data.
        files = sorted(str(path) for path in data.rglob("*.json") if path.is_file())
        for pos in range(0, len(files), 100):
            runner(["git", "add", "-f", "--", *files[pos:pos + 100]])


def publish(runner: Callable[..., int] = run, max_attempts: int = MAX_ATTEMPTS) -> bool:
    """Return True on publication, False if there was nothing to publish.

    `runner` is injectable so push races and failure paths have offline tests.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")

    runner(["git", "config", "user.name", "github-actions[bot]"])
    runner(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])

    for attempt in range(1, max_attempts + 1):
        print(f"\n=== LJLM publication attempt {attempt}/{max_attempts} ===", flush=True)
        # Never process data from the workflow event's potentially stale SHA.
        # These reset/clean calls are safe only in the disposable Actions clone.
        runner(["git", "fetch", "--no-tags", "origin", GIT_TARGET])
        runner(["git", "reset", "--hard", f"origin/{GIT_TARGET}"])
        runner(["git", "clean", "-fd", "--", *CLEAN_DIRS])
        runner(["git", "status", "--short"])

        for title, argv in PIPELINE:
            print(f"::group::{title}", flush=True)
            try:
                runner(argv)
            finally:
                print("::endgroup::", flush=True)

        stage_products(runner)
        if runner(["git", "diff", "--cached", "--quiet"], allow_failure=True) == 0:
            print("No LJLM changes; fresh main is already current.", flush=True)
            return False

        runner(["git", "diff", "--cached", "--check"])
        runner(["git", "commit", "-m", "Update LJLM sounding"])
        if runner(["git", "push", "origin", "HEAD:main"], allow_failure=True) == 0:
            print("LJLM publication completed successfully.", flush=True)
            return True

        if attempt < max_attempts:
            print("Git push failed (possibly a concurrent update). ",
                  "Rebuilding all generated files from the new origin/main; "
                  "no generated-file rebase will be attempted.", flush=True)

    raise RuntimeError(f"Failed to publish LJLM after {max_attempts} fresh-base attempts; "
                       "no force push performed")


def main() -> int:
    # Guard against accidental execution in the developer's normal worktree.
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REF") != "refs/heads/main":
        print("Refusing destructive sync outside GitHub Actions main-branch checkout.", file=sys.stderr)
        return 2
    publish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
