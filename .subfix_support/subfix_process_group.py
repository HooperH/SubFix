#!/usr/bin/env python3
"""Run one shell command in a dedicated process group."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


def process_table() -> dict[int, tuple[int, int, str]]:
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,pgid=,stat="],
        check=False,
        text=True,
        capture_output=True,
        timeout=3.0,
    )
    if result.returncode != 0:
        raise RuntimeError("ps failed while stopping SubFix process groups")
    rows: dict[int, tuple[int, int, str]] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 4:
            raise RuntimeError("invalid ps output while stopping SubFix process groups")
        pid, ppid, pgid = (int(value) for value in fields[:3])
        rows[pid] = (ppid, pgid, fields[3])
    return rows


def descendant_process_groups(root_pid: int) -> list[int]:
    rows = process_table()
    if root_pid not in rows:
        return []
    root_pgid = rows[root_pid][1]
    own_pgid = os.getpgrp()
    if root_pgid != root_pid or root_pgid == own_pgid:
        raise RuntimeError("target is not a dedicated SubFix session leader")
    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _pgid, _stat) in rows.items():
            if ppid in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    groups = {rows[pid][1] for pid in descendants}
    safe_groups = [pgid for pgid in groups if pgid > 1 and pgid != own_pgid]
    return sorted(safe_groups, key=lambda pgid: pgid == root_pgid)


def signal_groups(groups: list[int], signum: int) -> None:
    active_groups = {
        pgid
        for _pid, (_ppid, pgid, stat) in process_table().items()
        if not stat.startswith("Z")
    }
    for pgid in groups:
        if pgid not in active_groups:
            continue
        try:
            os.killpg(pgid, signum)
        except ProcessLookupError:
            pass
        except PermissionError:
            still_active = any(
                row_pgid == pgid and not stat.startswith("Z")
                for _pid, (_ppid, row_pgid, stat) in process_table().items()
            )
            if still_active:
                raise


def stop_process_tree(root_pid: int) -> int:
    groups = descendant_process_groups(root_pid)
    if not groups:
        return 0
    signal_groups(groups, signal.SIGTERM)
    time.sleep(0.2)
    signal_groups(groups, signal.SIGKILL)
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) == 2 and arguments[0] == "--stop":
        try:
            root_pid = int(arguments[1])
            if root_pid <= 1:
                raise ValueError
            return stop_process_tree(root_pid)
        except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
            print(f"failed to stop SubFix process tree: {exc}", file=sys.stderr)
            return 1
    if len(arguments) != 1:
        print("usage: subfix_process_group.py COMMAND | --stop PID", file=sys.stderr)
        return 2
    os.setsid()
    os.execl("/bin/sh", "sh", "-c", arguments[0])
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
