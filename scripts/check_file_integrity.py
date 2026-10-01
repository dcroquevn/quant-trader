"""Find tracked source files that have been zeroed out or truncated.

    python scripts/check_file_integrity.py

Exits 1 if anything is damaged, so it can be wired into a pre-commit hook or CI.

Why this exists
---------------
Two source files in this repository were once found to be entirely NUL bytes -- 97,163 and 10,503
of them, every byte. That is the signature of a write interrupted after the filesystem allocated
blocks but before it flushed the data, and this project is on a OneDrive-synced path, which makes
it more likely rather than less.

The failure is nastier than it sounds because of how the tools react:

* Python reports ``SyntaxError: invalid syntax`` at line 1, which reads like a code mistake.
* ``git diff`` says "Binary files differ" and shows nothing, so the damage is invisible in review.
* ``grep`` silently finds no matches, because it treats the file as binary.

Each of those points away from the real cause. A direct check is faster than deducing it again.

Recovery is ``git checkout -- <path>`` for anything committed, which is why committing often is the
actual mitigation. This script only tells you which files to restore.
"""

import subprocess
import sys


def main() -> int:
    tracked = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    ).stdout.splitlines()

    damaged: list[tuple[str, int | None, int | None]] = []
    healthy = 0

    for name in tracked:
        try:
            data = open(name, "rb").read()
        except FileNotFoundError:
            # Tracked but absent. Not corruption, but the same fix applies.
            damaged.append((name, None, None))
            continue
        nuls = data.count(b"\x00")
        if nuls:
            damaged.append((name, len(data), nuls))
        else:
            healthy += 1

    print(f"{healthy} tracked files intact")
    if not damaged:
        print("no corruption found")
        return 0

    print(f"\n{len(damaged)} file(s) damaged:")
    for name, size, nuls in damaged:
        if size is None:
            print(f"  {name}  MISSING")
        else:
            share = nuls / size * 100 if size else 100.0
            print(f"  {name}  {size} bytes, {nuls} NUL ({share:.0f}%)")

    print("\nRestore anything committed with:")
    print("  git checkout -- " + " ".join(name for name, _, _ in damaged))
    print("\nA file not yet committed has to be rewritten; there is nothing to restore from.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
