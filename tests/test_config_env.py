import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config


def test_load_project_env_only_reads_repository_local_file(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (tmp_path / ".env").write_text("PARENT_ONLY=leaked\n", encoding="utf-8")
    (project_dir / "rss-ingest-local.env").write_text("PROJECT_ONLY=loaded\n", encoding="utf-8")
    monkeypatch.delenv("PARENT_ONLY", raising=False)
    monkeypatch.delenv("PROJECT_ONLY", raising=False)

    config.load_project_env(project_dir)

    assert os.getenv("PROJECT_ONLY") == "loaded"
    assert os.getenv("PARENT_ONLY") is None
