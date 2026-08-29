"""Run the prepared Result Memory Default hotfix against the real production checkout.

This wrapper requires the real production cwd, reads the prepared V3 patcher
from the dedicated tournament branch, overrides only the patcher's ROOT
binding in memory, and executes it without writing the patcher into the repo.

It never performs git reset/stash/checkout/commit/push and never touches the
production DB.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REMOTE_REF = "origin/tournament-production-20260829"
PATCHER_PATH = "scripts/apply_tournament_result_memory_default_hotfix_v3.py"
EXPECTED_PRODUCTION_ROOT = Path(r"C:\work\maple-next")
AUTHORIZED_TARGETS = (
    Path("src/maple_next/ui/battle_record_ui.py"),
    Path("tests/test_tournament_p0_result_entry_redesign.py"),
)
ROOT_BINDING = "ROOT = Path(__file__).resolve().parents[1]"


def _normalized(path: Path) -> str:
    return str(path.resolve()).casefold()


def main() -> int:
    root = Path.cwd().resolve()
    if _normalized(root) != _normalized(EXPECTED_PRODUCTION_ROOT):
        raise RuntimeError(
            "WRONG_CWD: run from C:\\work\\maple-next; "
            f"actual={root}"
        )
    missing = [str(path) for path in AUTHORIZED_TARGETS if not (root / path).is_file()]
    if missing:
        raise RuntimeError(f"AUTHORIZED_TARGET_MISSING:{','.join(missing)}")

    completed = subprocess.run(
        ["git", "show", f"{REMOTE_REF}:{PATCHER_PATH}"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    source = completed.stdout
    if source.count(ROOT_BINDING) != 1:
        raise RuntimeError(
            "PATCHER_ROOT_BINDING_UNEXPECTED: expected exactly one prepared ROOT binding"
        )
    source = source.replace(ROOT_BINDING, f"ROOT = Path({str(root)!r})", 1)

    namespace: dict[str, object] = {
        "__name__": "_maple_prepared_result_memory_hotfix_v3",
        "__file__": str(root / PATCHER_PATH),
    }
    exec(compile(source, PATCHER_PATH, "exec"), namespace)
    runner = namespace.get("main")
    if not callable(runner):
        raise RuntimeError("PATCHER_MAIN_MISSING")
    result = runner()
    return int(result) if result is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
