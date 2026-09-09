#!/usr/bin/env python3
"""Run one shell command in a dedicated process group."""

from __future__ import annotations

import os
import sys


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("usage: subfix_process_group.py COMMAND", file=sys.stderr)
        return 2
    os.setsid()
    os.execl("/bin/sh", "sh", "-c", arguments[0])
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
