#!/usr/bin/env python3
import sys
from pathlib import Path

def escape_for_ssh_double_quotes(text: str) -> str:
    # bash / ssh double-quote safe escaping
    text = text.replace("\\", "\\\\")
    text = text.replace('"', '\\"')
    text = text.replace("$", "\\$")
    return text

def main():
    if len(sys.argv) < 2:
        print("usage: escape.py <script.sh>", file=sys.stderr)
        sys.exit(1)

    path = Path(sys.argv[1])
    raw = path.read_text()

    escaped = escape_for_ssh_double_quotes(raw)

    # 1行化（sshコマンド用）
    print(escaped, end="")

if __name__ == "__main__":
    main()
    print()  # ensure final newline