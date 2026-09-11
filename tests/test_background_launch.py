"""Exercise the Lua launch command with host-owned output pipes."""

from pathlib import Path
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BackgroundLaunchTests(unittest.TestCase):
    def test_stop_mode_rejects_non_session_leader(self):
        runner_path = ROOT / ".subfix_support/subfix_process_group.py"
        result = subprocess.run(
            [sys.executable, str(runner_path), "--stop", str(os.getpid())],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("session leader", result.stderr)

    def test_cancel_prefers_process_tree_stop_with_legacy_fallback(self):
        source = (ROOT / ".subfix_support/subfix_generate_selection_core.lua").read_text()
        cancel = source.split("local function kill_background_process(", 1)[1].split("\nend", 1)[0]
        self.assertIn('"--stop"', cancel)
        self.assertIn("kill -TERM -- -", cancel)

    def test_launcher_releases_host_pipes_before_worker_finishes(self):
        source = (ROOT / ".subfix_support/subfix_generate_selection_core.lua").read_text()
        runner = source.split("local function run_background_command_with_progress(", 1)[1]
        template = re.search(r'local bg_cmd = string.format\(\s*"([^"\n]+)"', runner).group(1)
        with tempfile.TemporaryDirectory(prefix="subfix-launch-test-") as folder:
            root = Path(folder)
            release = root / "release"
            code = (
                "import pathlib,time,sys; "
                f"release=pathlib.Path({str(release)!r}); "
                "deadline=time.monotonic()+10; "
                "print('worker started',flush=True)\n"
                "while not release.exists() and time.monotonic()<deadline: time.sleep(.02)\n"
                "sys.exit(7)\n"
            )
            command = shlex.join([sys.executable, "-c", code])
            grouped = shlex.join([
                sys.executable, str(ROOT / ".subfix_support/subfix_process_group.py"), command,
            ])
            log, pid, done, status = [root / name for name in ("log", "pid", "done", "exit")]
            launch = template % (grouped, *(shlex.quote(str(p)) for p in (log, pid, status, done)))
            host = subprocess.Popen(["/bin/sh", "-c", launch], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                try:
                    host.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    self.fail("background wrapper keeps host output pipes open until installation ends")
                self.assertEqual(host.returncode, 0)
                self.assertFalse(done.exists(), "worker must still be running when launcher returns")
            finally:
                release.touch()
                host.communicate(timeout=12)
                deadline = time.monotonic() + 3
                while not done.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
            self.assertTrue(done.exists(), "completion sentinel must still be written")
            self.assertEqual(status.read_text().strip(), "7", "worker exit code must survive detachment")
            self.assertIn("worker started", log.read_text())

    def test_stop_mode_kills_nested_stage_process_group(self):
        runner_path = ROOT / ".subfix_support/subfix_process_group.py"
        with tempfile.TemporaryDirectory(prefix="subfix-stop-test-") as folder:
            pid_path = Path(folder) / "pids"
            stage_code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); time.sleep(30)"
            root_code = (
                "import pathlib,subprocess,sys,time; "
                f"p=subprocess.Popen([sys.executable,'-c',{stage_code!r}],start_new_session=True); "
                f"pathlib.Path({str(pid_path)!r}).write_text(str(p.pid)); "
                "time.sleep(30)"
            )
            command = shlex.join([sys.executable, "-c", root_code])
            root = subprocess.Popen([sys.executable, str(runner_path), command])
            try:
                deadline = time.monotonic() + 5
                while not pid_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(pid_path.exists())
                stage_pid = int(pid_path.read_text())
                stopped = subprocess.run(
                    [sys.executable, str(runner_path), "--stop", str(root.pid)],
                    timeout=5,
                )
                self.assertEqual(stopped.returncode, 0)
                root.wait(timeout=3)
                with self.assertRaises(ProcessLookupError):
                    os.kill(stage_pid, 0)
            finally:
                for pid in (root.pid, int(pid_path.read_text()) if pid_path.exists() else 0):
                    if pid:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass


if __name__ == "__main__":
    unittest.main()
