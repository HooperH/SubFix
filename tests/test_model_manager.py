import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SUPPORT = ROOT / ".subfix_support"


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.syspath_prepend(str(SUPPORT))
    spec = importlib.util.spec_from_file_location("subfix_model_manager_test", SUPPORT / "subfix_model_manager.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.runtime, "huggingface_cache_roots", lambda: [])
    manifests = []
    for name, hidden in zip(module.ASR_IDS, (1024, 2048)):
        files = {
            "config.json": json.dumps({"model_type": "qwen3_asr", "thinker_config": {"text_config": {"hidden_size": hidden}}}).encode(),
            "model.safetensors": b"small-weight-fixture-" + str(hidden).encode(),
            "tokenizer_config.json": b"{}",
        }
        manifests.append({"repo": "Qwen/" + name, "revision": "fixed-revision",
                          "files": {key: {"size": len(data), "hash": hashlib.sha256(data).hexdigest(), "algorithm": "sha256"}
                                    for key, data in files.items()}, "_fixture": files})
    monkeypatch.setattr(module, "ASR_MANIFESTS", manifests)
    aligner = (struct.pack("<4sIQQ", b"GGUF", 3, 708, 34)
               + b"qwen3-asr.audio.encoder.layer_count\0qwen3-asr.classify_num\0")
    monkeypatch.setattr(module, "ALIGNER_SIZE", len(aligner))
    monkeypatch.setattr(module, "ALIGNER_SHA256", hashlib.sha256(aligner).hexdigest())
    module.aligner_fixture = aligner
    return module


@pytest.fixture
def paths(manager, tmp_path):
    return manager.ModelPaths(tmp_path / "plugin", tmp_path / "data")


def write_asr(manager, path, model_id):
    path.mkdir(parents=True, exist_ok=True)
    for name, data in manager.spec_for(model_id)["_fixture"].items():
        (path / name).write_bytes(data)
    return path


def ready_env(paths):
    rp = paths.runtime_paths
    rp.env_python.parent.mkdir(parents=True, exist_ok=True)
    rp.env_python.touch()
    rp.ready_marker.write_text("{}")


def write_aligner(manager, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(manager.aligner_fixture)
    return path


def test_catalog_is_offline_and_does_not_import_torch(manager, paths, monkeypatch):
    monkeypatch.setattr(manager.runtime, "python_can_import_qwen_asr", lambda *_: pytest.fail("no import"))
    monkeypatch.setattr(manager.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("no process or network"))
    result = manager.list_models(paths)
    assert result["selected_model_id"] == manager.ASR_IDS[0]
    assert len(result["models"]) == 3
    assert all(not item["ready"] for item in result["models"])
    assert result["models"][2]["download_available"] is False


def test_import_preserves_source_and_remains_offline_without_environment(manager, paths, tmp_path, monkeypatch):
    source = write_asr(manager, tmp_path / "downloaded", manager.ASR_IDS[0])
    monkeypatch.setattr(manager, "ensure_environment", lambda *_: pytest.fail("offline import must not install"))
    result = manager.import_model(paths, manager.ASR_IDS[0], source, lambda *_args, **_kwargs: None)
    assert result["model_installed"] is True
    assert result["state"] == "environment_missing"
    assert result["ready"] is False
    assert source.is_dir()
    assert manager.validate_model(paths.models / manager.ASR_IDS[0], manager.ASR_IDS[0], full=True)


def test_aligner_import_does_not_need_asr_runtime(manager, paths, tmp_path):
    source = write_aligner(manager, tmp_path / "download.gguf")
    result = manager.import_model(paths, manager.ALIGNER_ID, source, lambda *_args, **_kwargs: None)
    assert result["ready"]
    status = manager.runtime_status(paths, manager.ALIGNER_ID)
    assert status["ready"] and status["aligner"].endswith(manager.ALIGNER_FILENAME)
    assert not paths.runtime_paths.env_dir.exists()


def test_wrong_size_model_and_corrupt_weights_are_rejected(manager, paths, tmp_path):
    source = write_asr(manager, tmp_path / "wrong", manager.ASR_IDS[1])
    with pytest.raises(RuntimeError, match="不匹配"):
        manager.import_model(paths, manager.ASR_IDS[0], source, lambda *_args, **_kwargs: None)
    source = write_asr(manager, tmp_path / "corrupt", manager.ASR_IDS[0])
    weight = source / "model.safetensors"
    weight.write_bytes(b"x" * weight.stat().st_size)
    with pytest.raises(RuntimeError, match="不匹配"):
        manager.import_model(paths, manager.ASR_IDS[0], source, lambda *_args, **_kwargs: None)
    assert not paths.models.exists()


def test_crispasr_gguf_is_rejected_even_with_correct_extension(manager, paths, tmp_path):
    source = tmp_path / manager.ALIGNER_FILENAME
    source.write_bytes(b"GGUF" + b"x" * (manager.ALIGNER_SIZE - 4))
    assert not manager.validate_model(source, manager.ALIGNER_ID)
    with pytest.raises(RuntimeError):
        manager.import_model(paths, manager.ALIGNER_ID, source, lambda *_args, **_kwargs: None)


def test_existing_legacy_models_are_reused_without_copying(manager, paths, monkeypatch):
    old = write_asr(manager, paths.root / "models" / manager.ASR_IDS[1], manager.ASR_IDS[1])
    write_aligner(manager, paths.root / "models" / manager.ALIGNER_FILENAME)
    ready_env(paths)
    monkeypatch.setattr(manager, "download_file", lambda *_: pytest.fail("must reuse"))
    result = manager.install_model(paths, manager.ASR_IDS[1], "auto", lambda *_args, **_kwargs: None)
    assert result["ready"] and result["model_path"] == str(old)
    assert manager.selected_model_id(paths) == manager.ASR_IDS[1]
    assert manager.runtime_status(paths)["ready"]


def test_select_persists_uninstalled_model_without_download(manager, paths, monkeypatch):
    monkeypatch.setattr(manager, "download_file", lambda *_: pytest.fail("selection cannot download"))
    result = manager.select_model(paths, manager.ASR_IDS[1])
    assert result["selected_model_id"] == manager.ASR_IDS[1]
    assert manager.selected_model_id(paths) == manager.ASR_IDS[1]
    assert result["missing"] == [manager.ASR_IDS[1], "qwen-runtime", manager.ALIGNER_ID]
    with pytest.raises(ValueError):
        manager.select_model(paths, manager.ALIGNER_ID)


def test_environment_only_install_reuses_imported_model(manager, paths, monkeypatch):
    write_asr(manager, paths.models / manager.ASR_IDS[0], manager.ASR_IDS[0])
    monkeypatch.setattr(manager, "ensure_environment", lambda *_: ready_env(paths))
    monkeypatch.setattr(manager, "download_file", lambda *_: pytest.fail("must not redownload"))
    assert manager.install_model(paths, manager.ASR_IDS[0], "auto", lambda *_args, **_kwargs: None)["ready"]


def test_environment_preparation_reuses_unmarked_legacy_environment(manager, paths, monkeypatch):
    old_python = paths.root / ".subfix_asr_env" / "bin" / "python"
    old_python.parent.mkdir(parents=True)
    old_python.touch()
    assert manager.environment_python(paths) is None
    probes = []
    monkeypatch.setattr(manager.runtime, "python_can_import_qwen_asr", lambda path: probes.append(path) or True)
    monkeypatch.setattr(manager.runtime, "install_qwen_dependencies", lambda *_: pytest.fail("reuse existing environment"))
    assert manager.ensure_environment(paths, lambda *_: None) == old_python
    assert probes == [old_python]
    monkeypatch.setattr(manager.runtime, "python_can_import_qwen_asr", lambda path: pytest.fail("status cannot import torch"))
    assert manager.environment_python(paths) == old_python


def test_download_source_fallback_preserves_verified_files(manager, paths, monkeypatch):
    ready_env(paths)
    calls = []
    def download(url, destination, entry, report, log):
        calls.append(url)
        if "modelscope" in url and destination.name == "model.safetensors":
            raise RuntimeError("network disconnected")
        if destination.name == "config.json" and destination.exists():
            assert destination.read_bytes() == manager.ASR_MANIFESTS[0]["_fixture"]["config.json"]
        destination.write_bytes(manager.ASR_MANIFESTS[0]["_fixture"][destination.name])
    monkeypatch.setattr(manager, "download_file", download)
    result = manager.install_model(paths, manager.ASR_IDS[0], "auto", lambda *_args, **_kwargs: None)
    assert result["ready"]
    assert any("modelscope" in url for url in calls)
    assert any("huggingface" in url and "fixed-revision" in url for url in calls)
    assert not (paths.models / manager.ALIGNER_FILENAME).exists()


def test_failed_download_keeps_partial_and_does_not_publish(manager, paths, monkeypatch):
    ready_env(paths)
    def fail(url, destination, entry, report, log):
        destination.with_suffix(".part").write_bytes(b"partial")
        raise RuntimeError("offline")
    monkeypatch.setattr(manager, "download_file", fail)
    with pytest.raises(RuntimeError, match="离线导入"):
        manager.install_model(paths, manager.ASR_IDS[0], "auto", lambda *_args, **_kwargs: None)
    assert list((paths.staging / manager.ASR_IDS[0]).glob("*.part"))
    assert not (paths.models / manager.ASR_IDS[0]).exists()


def test_aligner_online_requires_verified_distributor_config(manager, paths):
    paths.root.mkdir(parents=True)
    config = {manager.ALIGNER_ID: {"sources": {"publisher": "https://example.org/model.gguf"},
                                  "size": manager.ALIGNER_SIZE, "sha256": manager.ALIGNER_SHA256}}
    (paths.root / "model-downloads.json").write_text(json.dumps(config))
    assert manager.model_status(paths, manager.ALIGNER_ID)["sources"] == ["publisher"]
    config[manager.ALIGNER_ID]["sha256"] = "0" * 64
    (paths.root / "model-downloads.json").write_text(json.dumps(config))
    assert not manager.model_status(paths, manager.ALIGNER_ID)["download_available"]


@pytest.mark.parametrize("url", ["http://example.org/file", "https://user:secret@example.org/file", "file:///tmp/a"])
def test_distributor_config_rejects_unsafe_urls(manager, paths, url):
    paths.root.mkdir(parents=True)
    (paths.root / "model-downloads.json").write_text(json.dumps({
        manager.ALIGNER_ID: {"sources": {"publisher": url}, "size": manager.ALIGNER_SIZE,
                            "sha256": manager.ALIGNER_SHA256}}))
    assert manager.aligner_sources(paths) == {}


def test_completed_partial_is_verified_and_reused_without_network(manager, tmp_path, monkeypatch):
    destination = tmp_path / "model.bin"
    data = b"complete"
    destination.with_name("model.bin.part").write_bytes(data)
    entry = {"size": len(data), "hash": hashlib.sha256(data).hexdigest(), "algorithm": "sha256"}
    monkeypatch.setattr(manager.runtime, "run_checked", lambda *_args, **_kwargs: pytest.fail("no network"))
    manager.download_file("https://example.org/file", destination, entry, lambda *_: None, tmp_path / "log")
    assert destination.read_bytes() == data


def test_download_checksum_failure_quarantines_partial(manager, tmp_path, monkeypatch):
    destination = tmp_path / "model.bin"
    entry = {"size": 4, "hash": hashlib.sha256(b"good").hexdigest(), "algorithm": "sha256"}
    def run(command, **kwargs):
        Path(command[command.index("--output") + 1]).write_bytes(b"bad!")
    monkeypatch.setattr(manager.runtime, "run_checked", run)
    with pytest.raises(RuntimeError, match="校验失败"):
        manager.download_file("https://example.org/file", destination, entry, lambda *_: None, tmp_path / "log")
    assert not destination.exists()
    assert list(tmp_path.glob("*.invalid-*"))


def test_server_without_range_restarts_only_current_file(manager, tmp_path, monkeypatch):
    destination = tmp_path / "model.bin"
    destination.with_name("model.bin.part").write_bytes(b"go")
    entry = {"size": 4, "hash": hashlib.sha256(b"good").hexdigest(), "algorithm": "sha256"}
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert command[command.index("--max-filesize") + 1] == "4"
        if len(calls) == 1:
            raise RuntimeError("curl: (33) HTTP server does not support byte ranges")
        Path(command[command.index("--output") + 1]).write_bytes(b"good")
    monkeypatch.setattr(manager.runtime, "run_checked", run)
    manager.download_file("https://example.org/file", destination, entry, lambda *_: None, tmp_path / "log")
    assert destination.read_bytes() == b"good"
    assert len(calls) == 2
    assert list(tmp_path.glob("*.unresumable-*"))


def test_import_rejects_symlink_staging_without_touching_destination(manager, paths, tmp_path):
    source = write_asr(manager, tmp_path / "source", manager.ASR_IDS[0])
    outside = tmp_path / "outside"
    outside.mkdir()
    paths.data_root.mkdir()
    paths.staging.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="软链接"):
        manager.import_model(paths, manager.ASR_IDS[0], source, lambda *_args, **_kwargs: None)
    assert not list(outside.iterdir())


def test_publish_failure_restores_existing_install(manager, paths, tmp_path, monkeypatch):
    target = tmp_path / "model"
    target.mkdir()
    (target / "old").write_text("preserved")
    staging = tmp_path / "missing"
    with pytest.raises(OSError):
        manager.publish_staging(staging, target)
    assert (target / "old").read_text() == "preserved"


def test_cli_reports_import_error_in_output_json(manager, paths, tmp_path):
    output = tmp_path / "output.json"
    code = manager.main(["--action", "import", "--model-id", manager.ASR_IDS[0],
                         "--root", str(paths.root), "--data-root", str(paths.data_root),
                         "--output", str(output)])
    assert code == 1
    assert json.loads(output.read_text())["state"] == "error"


def test_git_blob_checksum_matches_official_metadata_algorithm(manager, tmp_path):
    path = tmp_path / "config.json"
    path.write_bytes(b"{}")
    entry = {"size": 2, "hash": hashlib.sha1(b"blob 2\0{}").hexdigest(), "algorithm": "git-sha1"}
    assert manager.file_matches(path, entry, full=True)


def test_import_repairs_same_size_corruption_in_existing_model(manager, paths, tmp_path):
    model_id = manager.ASR_IDS[0]
    installed = write_asr(manager, paths.models / model_id, model_id)
    source = write_asr(manager, tmp_path / "correct-model", model_id)
    weight = installed / "model.safetensors"
    weight.write_bytes(b"x" * weight.stat().st_size)
    result = manager.import_model(paths, model_id, source, lambda *_args, **_kwargs: None)
    assert result["model_installed"]
    assert manager.validate_model(installed, model_id, full=True)
    assert manager.validate_model(source, model_id, full=True)


def test_install_repairs_same_size_corruption_in_existing_model(manager, paths, monkeypatch):
    model_id = manager.ASR_IDS[0]
    installed = write_asr(manager, paths.models / model_id, model_id)
    weight = installed / "model.safetensors"
    weight.write_bytes(b"x" * weight.stat().st_size)
    ready_env(paths)
    calls = []
    def download(url, destination, entry, report, log):
        calls.append(url)
        destination.write_bytes(manager.spec_for(model_id)["_fixture"][destination.name])
    monkeypatch.setattr(manager, "download_file", download)
    assert manager.install_model(paths, model_id, "auto", lambda *_args, **_kwargs: None)["ready"]
    assert calls and manager.validate_model(installed, model_id, full=True)


def test_install_repairs_preferred_corrupt_copy_from_valid_legacy_copy(manager, paths, monkeypatch):
    model_id = manager.ASR_IDS[0]
    installed = write_asr(manager, paths.models / model_id, model_id)
    legacy = write_asr(manager, paths.root / "models" / model_id, model_id)
    weight = installed / "model.safetensors"
    weight.write_bytes(b"x" * weight.stat().st_size)
    ready_env(paths)
    monkeypatch.setattr(manager, "download_file", lambda *_: pytest.fail("repair from existing cache"))
    assert manager.install_model(paths, model_id, "auto", lambda *_args, **_kwargs: None)["ready"]
    assert manager.validate_model(installed, model_id, full=True)
    assert manager.validate_model(legacy, model_id, full=True)


def test_aligner_validation_works_without_download_source(manager, paths, monkeypatch):
    installed = write_aligner(manager, paths.models / manager.ALIGNER_FILENAME)
    monkeypatch.setattr(manager, "download_file", lambda *_: pytest.fail("validation does not download"))
    assert manager.install_model(paths, manager.ALIGNER_ID, "auto", lambda *_args, **_kwargs: None)["ready"]
    data = bytearray(installed.read_bytes())
    data[12] ^= 1
    installed.write_bytes(data)
    with pytest.raises(RuntimeError, match="导入"):
        manager.install_model(paths, manager.ALIGNER_ID, "auto", lambda *_args, **_kwargs: None)


@pytest.mark.parametrize("relative", ["", "nested"])
def test_import_rejects_overlapping_staging_without_modifying_source(manager, paths, relative):
    model_id = manager.ASR_IDS[0]
    staging = paths.staging / (model_id + "-import")
    source = write_asr(manager, staging / relative, model_id)
    before = {name: (source / name).read_bytes() for name in manager.spec_for(model_id)["files"]}
    with pytest.raises(RuntimeError, match="重叠"):
        manager.import_model(paths, model_id, source, lambda *_args, **_kwargs: None)
    assert before == {name: (source / name).read_bytes() for name in before}


def test_import_rejects_hardlinked_destination_before_any_copy(manager, paths, tmp_path):
    model_id = manager.ASR_IDS[0]
    source = write_asr(manager, tmp_path / "source", model_id)
    staging = paths.staging / (model_id + "-import")
    staging.mkdir(parents=True)
    (staging / "model.safetensors").hardlink_to(source / "model.safetensors")
    with pytest.raises(RuntimeError, match="重叠"):
        manager.import_model(paths, model_id, source, lambda *_args, **_kwargs: None)
    assert manager.validate_model(source, model_id, full=True)
    assert not (staging / "config.json").exists()


def test_quarantined_files_do_not_count_toward_progress_or_free_space(manager, paths, monkeypatch):
    model_id = manager.ASR_IDS[0]
    ready_env(paths)
    staging = paths.staging / model_id
    staging.mkdir(parents=True)
    files = manager.spec_for(model_id)["files"]
    total = sum(entry["size"] for entry in files.values())
    (staging / "model.safetensors.part.invalid-1").write_bytes(b"x" * total)
    (staging / "model.safetensors.part.unresumable-1").write_bytes(b"x" * total)
    assert manager.reusable_download_bytes(staging, files) == 0
    usage = manager.shutil.disk_usage(staging)
    monkeypatch.setattr(manager.shutil, "disk_usage", lambda _: type(usage)(usage.total, usage.used, 256 * 1024 * 1024 + 1))
    monkeypatch.setattr(manager, "download_file", lambda *_: pytest.fail("must reject before downloading"))
    with pytest.raises(RuntimeError, match="空间不足"):
        manager.install_model(paths, model_id, "auto", lambda *_args, **_kwargs: None)


def test_download_cache_only_counts_reusable_manifest_files(manager, paths):
    model_id = manager.ASR_IDS[0]
    staging = paths.staging / model_id
    staging.mkdir(parents=True)
    files = manager.spec_for(model_id)["files"]
    valid = manager.spec_for(model_id)["_fixture"]["config.json"]
    (staging / "config.json").write_bytes(valid)
    (staging / "model.safetensors.part").write_bytes(b"part")
    (staging / "tokenizer_config.json").write_bytes(b"xx")
    assert manager.reusable_download_bytes(staging, files, full=True) == len(valid) + 4


@pytest.mark.parametrize("corrupt", [False, True])
def test_importing_current_model_path_never_changes_source(manager, paths, corrupt):
    model_id = manager.ASR_IDS[0]
    source = write_asr(manager, paths.models / model_id, model_id)
    weight = source / "model.safetensors"
    if corrupt:
        weight.write_bytes(b"x" * weight.stat().st_size)
    before = {name: ((source / name).read_bytes(), (source / name).stat().st_mtime_ns)
              for name in manager.spec_for(model_id)["files"]}
    if corrupt:
        with pytest.raises(RuntimeError, match="源文件未修改"):
            manager.import_model(paths, model_id, source, lambda *_args, **_kwargs: None)
    else:
        assert manager.import_model(paths, model_id, source, lambda *_args, **_kwargs: None)["model_installed"]
    assert before == {name: ((source / name).read_bytes(), (source / name).stat().st_mtime_ns)
                      for name in before}


def test_full_validation_reports_heartbeat_and_stops_it(manager, paths, monkeypatch):
    stopped = []
    class Stop:
        def set(self):
            stopped.append(True)
    class Monitor:
        def join(self, timeout):
            assert timeout == 2
    def heartbeat(report, labels):
        assert labels[0] == "校验模型"
        return Stop(), Monitor()
    monkeypatch.setattr(manager.runtime, "start_command_heartbeat", heartbeat)
    with manager.validation_progress(lambda *_: None, "正在校验"):
        assert stopped == []
    assert stopped == [True]
