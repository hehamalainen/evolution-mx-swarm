from __future__ import annotations

import json
from pathlib import Path

from shared.utils.config import MXConfig
from worker.service import Worker


class _StubQueue:
    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None


class _StubLLM:
    async def chat(self, *args, **kwargs):
        raise AssertionError("LLM should not be called in this unit test")


def test_worker_materializes_local_landing_and_closes_mock_submission(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    source_root = tmp_path / "source"
    workspace_root = tmp_path / "workspace"
    (source_root / "tools").mkdir(parents=True)
    (workspace_root / "tools").mkdir(parents=True)

    (source_root / "tools" / "namecore.py").write_text("old\n", encoding="utf-8")
    (workspace_root / "tools" / "namecore.py").write_text("new\n", encoding="utf-8")
    (source_root / "mock_prs.json").write_text(
        json.dumps(
            {
                "prs": [
                    {
                        "number": 7,
                        "state": "open",
                        "merged": False,
                        "merged_at": None,
                    }
                ],
                "next_id": 8,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    config = MXConfig()
    config.vcs.provider = "github"
    worker = Worker(
        config=config,
        worker_id="worker-test",
        queue=_StubQueue(),
        llm=_StubLLM(),
    )

    copied = worker._materialize_local_landing(
        workspace_root=str(workspace_root),
        source_root=str(source_root),
        changed_files=["tools/namecore.py"],
    )

    assert copied == ["tools/namecore.py"]
    assert (source_root / "tools" / "namecore.py").read_text(encoding="utf-8") == "new\n"

    monkeypatch.chdir(source_root)
    worker._close_local_mock_submission("7")
    payload = json.loads((source_root / "mock_prs.json").read_text(encoding="utf-8"))
    assert payload["prs"][0]["state"] == "closed"
    assert payload["prs"][0]["merged"] is True
    assert payload["prs"][0]["merge_detail"] == "materialized_local_landing"
