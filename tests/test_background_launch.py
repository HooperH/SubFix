"""Exercise the Lua launch command with host-owned output pipes."""

from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BackgroundLaunchTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
