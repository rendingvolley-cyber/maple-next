"""Tournament P0: enforce fixed-package Selection Profile at APPLY boundary.

Deterministic source patcher for the real production checkout. It edits only
src/maple_next/application/service.py and fails closed if the expected current
APPLY-selection block has drifted.
"""
from __future__ import annotations

from pathlib import Path

TARGET = Path("src/maple_next/application/service.py")

OLD = '''            if lead not in typed_three:\n                raise DomainError("LEAD_NOT_IN_SELECTED_THREE")\n\n            backline_values = tuple(name for name in typed_three if name != lead)\n'''

NEW = '''            if lead not in typed_three:\n                raise DomainError("LEAD_NOT_IN_SELECTED_THREE")\n\n            # Tournament P0 / maple-team.v3. Human APPLY may intentionally\n            # differ from Gemini advice, but it must still obey the bound\n            # human-authored Selection Profile. For fixed_packages with\n            # mixing_allowed=false, any cross-package trio is invalid battle\n            # state and must fail before the first durable applied-selection\n            # write. This is defense in depth in addition to provider-result\n            # validation; it also protects manual operator override paths.\n            selection_profile = (\n                selection_facts.self_team_build.selection_profile\n                if selection_facts.self_team_build is not None\n                else None\n            )\n            if selection_profile is not None and not selection_profile.mixing_allowed:\n                matching_package = next(\n                    (\n                        package\n                        for package in selection_profile.packages\n                        if set(typed_three) == set(package.members)\n                    ),\n                    None,\n                )\n                if matching_package is None:\n                    raise DomainError("SELECTION_MIXES_FIXED_PACKAGES")\n\n            backline_values = tuple(name for name in typed_three if name != lead)\n'''


def main() -> None:
    root = Path.cwd().resolve()
    expected_root = Path(r"C:\work\maple-next").resolve()
    if root != expected_root:
        raise RuntimeError(f"WRONG_PRODUCTION_ROOT:{root}")
    text = TARGET.read_text(encoding="utf-8")
    count = text.count(OLD)
    if count != 1:
        raise RuntimeError(f"APPLY_SELECTION_ANCHOR_EXPECTED_1_GOT_{count}")
    updated = text.replace(OLD, NEW, 1)
    TARGET.write_text(updated, encoding="utf-8")
    print("FIXED_PACKAGE_APPLY_GUARD_PATCHED")
    print(TARGET.as_posix())


if __name__ == "__main__":
    main()
