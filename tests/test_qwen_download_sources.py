"""Domestic downloads must not contact Hugging Face unless the primary fails."""

from pathlib import Path

import pytest

from test_qwen_environment_location import load_manager as load_manager_module


def make_paths(manager, root):
    return manager.SubFixQwenPaths(root, data_root=root / "subfix-user-data")


def test_dependency_commands_use_domestic_index_and_keep_resume_support(tmp_path, monkeypatch):
    manager = load_manager_module()
    commands = []
    monkeypatch.setattr(manager, "run_checked", lambda command, **kwargs: commands.append(command))

    manager.install_qwen_dependencies(Path("python"), lambda *_: None, tmp_path / "install.log")

    assert len(commands) == 2
    for command in commands:
        assert command[command.index("--index-url") + 1] == "https://pypi.tuna.tsinghua.edu.cn/simple"
        assert "--trusted-host" not in command
        assert "config" not in command
    assert "pip>=25.2" in commands[0]
    assert "--resume-retries" in commands[1]
    assert "modelscope" in commands[1]


@pytest.mark.parametrize("already_installed", [False, True])
def test_existing_environment_only_installs_missing_downloader(tmp_path, monkeypatch, already_installed):
    manager = load_manager_module()
    commands = []
    monkeypatch.setattr(manager.subprocess, "run", lambda *args, **kwargs:
                        manager.subprocess.CompletedProcess(args=[], returncode=0 if already_installed else 1))
    monkeypatch.setattr(manager, "run_checked", lambda command, **kwargs: commands.append(command))

    manager.ensure_modelscope_downloader(Path("python"), lambda *_: None, tmp_path / "install.log")

    assert len(commands) == (0 if already_installed else 1)
    if commands:
        assert "modelscope" in commands[0] and "--index-url" in commands[0]
        assert "torch" not in commands[0] and "qwen-asr" not in commands[0]


@pytest.mark.parametrize("primary_fails", [False, True])
def test_download_uses_matching_metadata_source_and_preserves_partial_files(tmp_path, monkeypatch, primary_fails):
    manager = load_manager_module()
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    partial = model_dir / "partial.download"
    partial.write_text("keep")
    downloads, metadata, messages, stopped_monitors = [], [], [], []
    monkeypatch.setattr(manager, "ensure_modelscope_downloader", lambda *_: None)
    monkeypatch.setattr(manager, "fetch_model_total_bytes", lambda python, source: metadata.append(source) or 100)
    monkeypatch.setattr(manager, "report_model_download_progress", lambda directory, total, report, stop:
                        stopped_monitors.append(stop))

    def run(command, **kwargs):
        script = command[2]
        source = "modelscope" if "from modelscope" in script else "huggingface"
        if source == "modelscope":
            assert "os.environ['MODELSCOPE_DOWNLOAD_INTRA_CLOUD'] = 'false'" in script
        downloads.append(source)
        assert partial.read_text() == "keep"
        assert str(model_dir) == command[-1]
        if source == "modelscope" and primary_fails:
            raise RuntimeError("primary unavailable")
        for name in manager.QWEN_ASR_REQUIRED_MODEL_FILES:
            (model_dir / name).write_text("model")

    monkeypatch.setattr(manager, "run_checked", run)
    manager.download_model(Path("python"), model_dir,
                           lambda stage, message, **details: messages.append(message), tmp_path / "install.log")

    expected = ["modelscope", "huggingface"] if primary_fails else ["modelscope"]
    assert downloads == metadata == expected
    assert all(stop.is_set() for stop in stopped_monitors)
    assert any("魔搭" in message for message in messages)
    assert any("Hugging Face" in message for message in messages) == primary_fails


def test_missing_primary_sdk_can_fall_back_without_reinstalling_qwen(tmp_path, monkeypatch):
    manager = load_manager_module()
    sources = []
    monkeypatch.setattr(manager, "ensure_modelscope_downloader", lambda *_:
                        (_ for _ in ()).throw(RuntimeError("mirror unavailable")))
    monkeypatch.setattr(manager, "download_model_from_source", lambda python, directory, report, log, source:
                        sources.append(source))

    manager.download_model(Path("python"), tmp_path, lambda *_: None, tmp_path / "install.log")

    assert sources == ["huggingface"]


def test_all_sources_fail_without_silent_success(tmp_path, monkeypatch):
    manager = load_manager_module()
    monkeypatch.setattr(manager, "ensure_modelscope_downloader", lambda *_: None)
    monkeypatch.setattr(manager, "download_model_from_source", lambda python, directory, report, log, source:
                        (_ for _ in ()).throw(RuntimeError(source + " unavailable")))

    with pytest.raises(RuntimeError) as error:
        manager.download_model(Path("python"), tmp_path, lambda *_: None, tmp_path / "install.log")
    assert "modelscope unavailable" in str(error.value)
    assert "huggingface unavailable" in str(error.value)


def test_incomplete_primary_download_triggers_fallback(tmp_path, monkeypatch):
    manager = load_manager_module()
    sources = []
    monkeypatch.setattr(manager, "ensure_modelscope_downloader", lambda *_: None)
    monkeypatch.setattr(manager, "fetch_model_total_bytes", lambda *_: None)
    monkeypatch.setattr(manager, "report_model_download_progress", lambda *_: None)

    def run(command, **kwargs):
        source = "modelscope" if "from modelscope" in command[2] else "huggingface"
        sources.append(source)
        if source == "huggingface":
            for name in manager.QWEN_ASR_REQUIRED_MODEL_FILES:
                (tmp_path / name).write_text("model")

    monkeypatch.setattr(manager, "run_checked", run)
    manager.download_model(Path("python"), tmp_path, lambda *_: None, tmp_path / "install.log")
    assert sources == ["modelscope", "huggingface"]


def test_installed_legacy_environment_is_not_migrated_or_redownloaded(tmp_path, monkeypatch):
    manager = load_manager_module()
    paths = make_paths(manager, tmp_path)
    old_python = paths.legacy_env_dir / "bin" / "python"
    old_python.parent.mkdir(parents=True)
    old_python.touch()
    paths.legacy_plugin_ready_marker.write_text("{}")
    paths.legacy_model_dir.mkdir(parents=True)
    for name in manager.QWEN_ASR_REQUIRED_MODEL_FILES:
        (paths.legacy_model_dir / name).write_text("model")
    monkeypatch.setattr(manager, "download_model", lambda *_: pytest.fail("must reuse existing installation"))
    monkeypatch.setattr(manager, "install_qwen_dependencies", lambda *_: pytest.fail("must not reinstall"))

    result = manager.install(paths, lambda *_: None)
    assert result["ready"] and result["python"] == str(old_python)
