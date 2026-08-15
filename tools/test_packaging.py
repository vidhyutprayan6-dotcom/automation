#!/usr/bin/env python3
"""
Checks that what git hands the Mac is actually runnable.

The project is edited on Windows and run on macOS, and two things silently
break in that hand-off:

  * the executable bit. Git stores it in the tree, and a file committed as
    100644 arrives on the Mac non-executable, so ./run.sh stops at
    "permission denied" and a double-clicked .command refuses to open.
  * line endings. A shell script that arrives with CRLF fails on the first
    line, because the carriage return becomes part of the interpreter name:
    /usr/bin/env: 'bash\\r': No such file or directory

Both are invisible on Windows, so this reads the staged tree the way a macOS
checkout would and fails if either has crept back in.
"""

from __future__ import annotations

import io
import subprocess
import sys
import tarfile
from typing import List

LAUNCHERS = {"run.sh", "run_bot.sh", "Run.command", "RunBot.command"}
TEXT_SUFFIXES = (".py", ".sh", ".command", ".txt", ".json", ".md")


def staged_tree_archive() -> bytes:
    tree = subprocess.run(
        ["git", "write-tree"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return subprocess.run(
        ["git", "archive", "--format=tar", tree], capture_output=True, check=True
    ).stdout


def main() -> int:
    problems: List[str] = []
    seen = set()

    with tarfile.open(fileobj=io.BytesIO(staged_tree_archive())) as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            data = handle.read()
            crlf = data.count(b"\r\n")
            name = member.name

            if name in LAUNCHERS:
                seen.add(name)
                executable = bool(member.mode & 0o111)
                shebang = data.split(b"\n", 1)[0].decode("utf-8", "replace")
                print(
                    f"{name:16s} mode={member.mode:o} "
                    f"{'executable' if executable else 'NOT EXECUTABLE':14s} "
                    f"CRLF={crlf:<4d} {shebang}"
                )
                if not executable:
                    problems.append(
                        f"{name} is committed without the executable bit; fix with "
                        f"'git update-index --chmod=+x {name}'"
                    )
                if not shebang.startswith("#!"):
                    problems.append(f"{name} has no shebang line")
            elif name.endswith(TEXT_SUFFIXES) and crlf:
                problems.append(f"{name} would reach macOS with {crlf} CRLF endings")

            if name in LAUNCHERS and crlf:
                problems.append(f"{name} would reach macOS with CRLF endings")

    for name in sorted(LAUNCHERS - seen):
        problems.append(f"{name} is not tracked by git")

    print("-" * 70)
    if problems:
        for p in problems:
            print("PROBLEM:", p)
        return 1
    print("Every launcher is executable, and every text file is LF.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
