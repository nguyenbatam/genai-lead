"""Sample apps must stay HTTP clients — no platform imports."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
IMPORT_RE = re.compile(r"^\s*(from|import)\s+(control|core|apps)\b")


def test_examples_do_not_import_platform_packages() -> None:
    offenders: list[str] = []
    for path in EXAMPLES.rglob("*.py"):
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if IMPORT_RE.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{line_no}: {line.strip()}")
    assert not offenders, "examples must not import control/core/apps:\n" + "\n".join(offenders)
