from pathlib import Path
import os
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class UninstallerTests(unittest.TestCase):
    def test_confirmed_removal_is_limited_to_subfix(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            utility = home / "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
            plugin = utility / "SubFix/SubFix.lua"
            plugin.parent.mkdir(parents=True)
            plugin.write_text("plugin")
            other = utility / "OtherPlugin.lua"
            other.write_text("keep")
            models = home / "Library/Application Support/SubFix/models/test.model"
            models.parent.mkdir(parents=True)
            models.write_text("keep model")
            dependency = home / "Library/Application Support/SubFix/envs/qwen-local/test.py"
            dependency.parent.mkdir(parents=True)
            dependency.write_text("dependency")
            command = home / "uninstall.command"
            # Isolate the system path too; this test must never touch a real install.
            source = (ROOT / "卸载_SubFix.command").read_text()
            source = source.replace(
                'SYSTEM_UTILITY="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"',
                'SYSTEM_UTILITY="$HOME/test-system-utility"',
            )
            self.assertIn('SYSTEM_UTILITY="$HOME/test-system-utility"', source)
            command.write_text(source)
            result = subprocess.run(["/bin/bash", str(command)], env={**os.environ, "HOME": folder},
                                    input="UNINSTALL\n", capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(plugin.parent.exists())
            self.assertFalse(dependency.parent.exists())
            self.assertEqual(other.read_text(), "keep")
            self.assertEqual(models.read_text(), "keep model")

    def test_build_delivers_signed_app_from_canonical_source(self):
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run(
                ["/bin/bash", str(ROOT / "build_uninstaller.sh")],
                env={**os.environ, "OUTPUT_DIR": folder}, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            app = Path(folder) / "卸载_SubFix.app"
            self.assertTrue(app.is_dir())
            self.assertTrue(os.access(app / "Contents/MacOS/applet", os.X_OK))
            self.assertEqual((app / "Contents/Resources/uninstall.sh").read_bytes(),
                             (ROOT / "卸载_SubFix.command").read_bytes())
            verified = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)],
                                      capture_output=True, text=True)
            self.assertEqual(verified.returncode, 0, verified.stderr)
            script = subprocess.check_output(["osadecompile", str(app / "Contents/Resources/Scripts/main.scpt")], text=True)
            self.assertIn('cancel button "取消"', script)
            self.assertIn('--needs-admin', script)
            self.assertIn('with administrator privileges', script)

    def test_admin_probe_is_read_only(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            source = (ROOT / "卸载_SubFix.command").read_text().replace(
                'SYSTEM_UTILITY="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"',
                'SYSTEM_UTILITY="$HOME/test-system-utility"',
            )
            self.assertIn('SYSTEM_UTILITY="$HOME/test-system-utility"', source)
            command = home / "uninstall.command"
            command.write_text(source)
            env = {**os.environ, "HOME": folder}
            args = ["/bin/bash", str(command), "--needs-admin"]
            self.assertEqual(subprocess.check_output(args, env=env, text=True).strip(), "no")
            system = home / "test-system-utility/SubFix"
            system.mkdir(parents=True)
            self.assertEqual(subprocess.check_output(args, env=env, text=True).strip(), "yes")
            self.assertTrue(system.is_dir())

    def test_preview_and_cancel_leave_installation_untouched(self):
        command = ROOT / "卸载_SubFix.command"
        self.assertIn("--dry-run", command.read_text())
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            install = home / "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
            plugin = install / "SubFix/SubFix.lua"
            plugin.parent.mkdir(parents=True)
            plugin.write_text("keep this plugin")
            env = {**os.environ, "HOME": folder}
            preview = subprocess.run(["/bin/bash", str(command), "--dry-run"], env=env, capture_output=True, text=True)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertIn(str(plugin.parent), preview.stdout)
            cancelled = subprocess.run(["/bin/bash", str(command)], env=env, input="no\n", capture_output=True, text=True)
            self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
            self.assertIn("已取消", cancelled.stdout)
            self.assertEqual(plugin.read_text(), "keep this plugin")


if __name__ == "__main__":
    unittest.main()
