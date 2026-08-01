#!/usr/bin/env python3
"""Regenerate the golden converter-source files under test/golden/.

Run after any intentional change to the code generator, then read the diff. A diff you
cannot explain is a bug in the generator, not a stale golden file.

Usage:
    python3 scripts/update_golden.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "test"))

from rosidl_runtime_py.utilities import get_message  # noqa: E402
from test_codegen import GOLDEN_TYPES  # noqa: E402

from ros2_http_gateway.codegen import dict_to_msg_source, msg_to_dict_source  # noqa: E402


def main() -> int:
    """Write one golden file per type in ``GOLDEN_TYPES``."""
    out_dir = REPO_ROOT / "test" / "golden"
    out_dir.mkdir(exist_ok=True)
    for type_name in GOLDEN_TYPES:
        cls = get_message(type_name)
        text = (
            f"# {type_name} msg_to_dict\n"
            f"{msg_to_dict_source(cls)}\n"
            f"# {type_name} dict_to_msg\n"
            f"{dict_to_msg_source(cls)}"
        )
        target = out_dir / f"{type_name.replace('/', '__')}.py.txt"
        target.write_text(text, encoding="utf-8")
        print(f"wrote {target.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
