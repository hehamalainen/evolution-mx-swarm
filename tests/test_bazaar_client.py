from __future__ import annotations

from types import SimpleNamespace

import pytest

from shared.utils.bazaar_client import MoltBazaarClient


@pytest.mark.asyncio
async def test_bazaar_client_is_safe_when_research_workspace_is_absent(monkeypatch) -> None:
    monkeypatch.delenv("MOLTBOOK_RESEARCH_ROOT", raising=False)

    client = MoltBazaarClient()

    assert client.enabled is False
    assert await client.post_task(SimpleNamespace(id="task-1", goal="demo", acceptance=SimpleNamespace(checks=[]))) == ""
    assert await client.get_bids("post-1") == []
    assert await client.get_comments("post-1") == []
    assert await client.create_comment("post-1", "hello") is False


def test_bazaar_client_formats_x402_header_without_external_dependency() -> None:
    client = MoltBazaarClient()
    payload = client.format_x402_header(10.0, "USDC", "wallet-1")

    assert "X-Payment-Request" in payload
    assert "USDC" in payload["X-Payment-Request"]
