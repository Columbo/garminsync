#!/usr/bin/env python3
"""Render a bash-safe env file from the repository's multiline .env format."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def parse_env(text: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    lines = text.splitlines()
    index = 0

    while index < len(lines):
        raw_line = lines[index]
        line = raw_line.strip()
        index += 1

        if not line or line.startswith("#"):
            continue

        match = ASSIGNMENT_RE.match(raw_line)
        if not match:
            raise ValueError(f"Unsupported line {index}: {raw_line!r}")

        key, value = match.groups()

        if value == "{":
            json_lines = ["{"]
            while index < len(lines):
                json_line = lines[index]
                json_lines.append(json_line)
                index += 1
                if json_line.strip() == "}":
                    break
            else:
                raise ValueError(f"Unterminated multiline JSON value for {key}")

            entries.append((key, "\n".join(json_lines)))
            continue

        entries.append((key, value))

    return entries


def render_bash(entries: list[tuple[str, str]]) -> str:
    output_lines = [
        "#!/usr/bin/env bash",
        "# Generated from .env by make_bash_env.py",
        "# shellcheck shell=bash",
        "",
    ]
    for key, value in entries:
        output_lines.append(f"export {key}={shell_quote(value)}")
    output_lines.append("")
    return "\n".join(output_lines)


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".env")
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".env.bash")

    entries = parse_env(src.read_text(encoding="utf-8"))
    dst.write_text(render_bash(entries), encoding="utf-8")
    print(f"Wrote bash-safe env file to {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
