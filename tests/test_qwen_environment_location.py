import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def load_manager():
    spec = importlib.util.spec_from_file_location("qwen_location_test", ROOT / ".subfix_support/subfix_qwen_local_manager.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class EnvironmentLocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = load_manager()
        self.paths = self.manager.SubFixQwenPaths(self.root / "resolve-scripts", self.root / "user-data")

    def make_model(self):
        self.paths.model_dir.mkdir(parents=True)
        for name in self.manager.QWEN_ASR_REQUIRED_MODEL_FILES:
            (self.paths.model_dir / name).write_text("model")

    def test_dependencies_and_ready_marker_are_outside_script_scan_root(self):
        self.assertEqual(self.paths.env_dir, self.paths.data_root / "envs/qwen-local")
        self.assertEqual(self.paths.ready_marker.parent, self.paths.data_root)
        self.assertFalse(self.paths.env_dir.is_relative_to(self.paths.root))

    def test_lua_manager_launches_suppress_runtime_bytecode_writes(self):
        source = (ROOT / ".subfix_support/subfix_generate_selection_core.lua").read_text()
        for name in ["build_qwen_status_command", "build_qwen_install_command"]:
            body = source.split("local function " + name, 1)[1].split("\nend", 1)[0]
            self.assertIn('"PYTHONDONTWRITEBYTECODE=1"', body)
            self.assertIn('"-B"', body)

    def test_real_local_pip_install_does_not_modify_script_tree(self):
        self.paths.base_python.parent.mkdir(parents=True)
        self.paths.base_python.symlink_to(sys.executable)
        before = {str(p.relative_to(self.paths.root)) for p in self.paths.root.rglob("*")}
        wheel = self.root / "subfix_path_probe-0.0.0-py3-none-any.whl"
        files = {f"subfix_path_probe/module_{i}.py": f"VALUE = {i}\n" for i in range(40)}
        files["subfix_path_probe/__init__.py"] = ""
        dist = "subfix_path_probe-0.0.0.dist-info/"
        files[dist + "METADATA"] = "Metadata-Version: 2.1\nName: subfix-path-probe\nVersion: 0.0.0\n"
        files[dist + "WHEEL"] = "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        files[dist + "RECORD"] = "".join(name + ",,\n" for name in [*files, dist + "RECORD"])
        with zipfile.ZipFile(wheel, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)

        def install_probe(python, report, log):
            subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--no-deps",
                            "--disable-pip-version-check", str(wheel)], capture_output=True, check=True)

        with patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1"}), \
             patch.object(self.manager, "python_can_import_qwen_asr", return_value=False), \
             patch.object(self.manager, "install_qwen_dependencies", side_effect=install_probe), \
             patch.object(self.manager, "existing_model_dir", side_effect=lambda _: self.paths.model_dir if self.manager.model_directory_is_complete(self.paths.model_dir) else None), \
             patch.object(self.manager, "download_model", side_effect=lambda *_: self.make_model()):
            result = self.manager.install(self.paths, lambda *_: None)
        self.assertTrue(result["ready"])
        self.assertTrue(list(self.paths.env_dir.rglob("module_39.py")))
        self.assertTrue(self.paths.ready_marker.is_file())
        self.assertEqual(before, {str(p.relative_to(self.paths.root)) for p in self.paths.root.rglob("*")})

    def test_completed_legacy_environment_still_works_without_mutation(self):
        self.make_model()
        legacy_python = self.paths.root / "envs/qwen-local/bin/python"
        legacy_python.parent.mkdir(parents=True)
        legacy_python.touch()
        marker = self.paths.root / ".subfix-qwen-local-ready.json"
        marker.write_text("{}")
        before = {str(p.relative_to(self.root)) for p in self.root.rglob("*")}
        status = self.manager.inspect_install(self.paths)
        self.assertTrue(status["ready"])
        self.assertEqual(status["python"], str(legacy_python))
        self.assertEqual(before, {str(p.relative_to(self.root)) for p in self.root.rglob("*")})

    def test_legacy_marker_never_certifies_new_incomplete_environment(self):
        self.make_model()
        self.paths.env_python.parent.mkdir(parents=True)
        self.paths.env_python.touch()
        self.paths.root.mkdir(exist_ok=True)
        (self.paths.root / ".subfix-qwen-local-ready.json").write_text("{}")
        self.assertFalse(self.manager.inspect_install(self.paths)["ready"])

    def test_new_ready_environment_is_preferred_to_legacy(self):
        self.make_model()
        self.paths.env_python.parent.mkdir(parents=True)
        self.paths.env_python.touch()
        self.manager.write_ready_marker(self.paths, self.paths.model_dir)
        legacy = self.paths.root / "envs/qwen-local/bin/python"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.touch()
        (self.paths.root / ".subfix-qwen-local-ready.json").write_text("{}")
        self.assertEqual(self.manager.inspect_install(self.paths)["python"], str(self.paths.env_python))


if __name__ == "__main__":
    unittest.main()
