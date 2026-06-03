from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BODY_PATH = ROOT / ".aiworker_guide_body.md"
TARGETS = {
    ROOT / "AGENTS.md": "# AIWorker Codex Guide",
    ROOT / "CLAUDE.md": "# AIWorker Claude Code Guide",
}


def render(title: str, body: str) -> str:
    return f"{title}\n\n{body.rstrip()}\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Synchronize AGENTS.md and CLAUDE.md from one shared guide body.")
    parser.add_argument("--check", action="store_true", help="Check whether generated guide files are up to date.")
    args = parser.parse_args()

    body = BODY_PATH.read_text(encoding="utf-8")
    changed = False
    for path, title in TARGETS.items():
        expected = render(title, body)
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current == expected:
            continue
        changed = True
        if args.check:
            diff = difflib.unified_diff(
                current.splitlines(keepends=True),
                expected.splitlines(keepends=True),
                fromfile=str(path),
                tofile=f"{path} (generated)",
            )
            sys.stdout.writelines(diff)
        else:
            path.write_text(expected, encoding="utf-8")
            print(f"synced {path.name}")

    if args.check and changed:
        return 1
    if not changed and not args.check:
        print("AGENTS.md and CLAUDE.md are already synced")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
