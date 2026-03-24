import json

from fastapi.testclient import TestClient

import dashboard.api as dashboard_api


def test_v2_analytics_endpoints_use_cached_payloads(monkeypatch, tmp_path) -> None:
    actor_map_dir = tmp_path / "vault" / "actor_map"
    actor_map_dir.mkdir(parents=True, exist_ok=True)
    phase_transition_path = tmp_path / "data" / "message_trends.json"
    phase_transition_path.parent.mkdir(parents=True, exist_ok=True)

    actor_payload = {
        "profile": {
            "id": "actor-1",
            "name": "Actor One",
        },
        "intelligence_scores": {
            "economic_mentions": 7,
            "cooperative_mentions": 3,
            "adversarial_mentions": 1,
            "alignment_score": 0.42,
        },
        "post_history": [{"id": "post-1"}, {"id": "post-2"}],
    }
    with open(actor_map_dir / "actor-1.json", "w") as handle:
        json.dump(actor_payload, handle)

    with open(phase_transition_path, "w") as handle:
        json.dump(
            {
                "2026-03-18": {
                    "Macro: Structural": 5,
                    "Macro: Ritual/Spam": 2,
                    "_post_count": 9,
                }
            },
            handle,
        )

    monkeypatch.setattr(dashboard_api, "_v2_actor_map_dir", lambda: str(actor_map_dir))
    monkeypatch.setattr(
        dashboard_api,
        "_v2_phase_transition_path",
        lambda: str(phase_transition_path),
    )
    dashboard_api._v2_analytics_cache.clear()

    with TestClient(dashboard_api.app) as client:
        economic = client.get("/api/v2/economic-flow")
        assert economic.status_code == 200
        assert economic.json() == [
            {
                "id": "actor-1",
                "name": "Actor One",
                "economic_mentions": 7,
            }
        ]

        alignment = client.get("/api/v2/alignment-matrix")
        assert alignment.status_code == 200
        assert alignment.json() == [
            {
                "id": "actor-1",
                "name": "Actor One",
                "cooperative": 3,
                "adversarial": 1,
                "alignment_score": 0.42,
                "total_mentions": 2,
            }
        ]

        phase = client.get("/api/v2/phase-transition")
        assert phase.status_code == 200
        assert phase.json() == {
            "2026-03-18": {
                "Structural": 5,
                "Ritual": 2,
                "_post_count": 9,
            }
        }

        with open(actor_map_dir / "actor-1.json", "w") as handle:
            json.dump(
                {
                    "profile": {"id": "actor-1", "name": "Actor One Updated"},
                    "intelligence_scores": {
                        "economic_mentions": 0,
                        "cooperative_mentions": 0,
                        "adversarial_mentions": 0,
                        "alignment_score": 0.0,
                    },
                    "post_history": [],
                },
                handle,
            )
        with open(phase_transition_path, "w") as handle:
            json.dump({}, handle)

        # Cached responses should stay stable for the active demo session.
        assert client.get("/api/v2/economic-flow").json() == economic.json()
        assert client.get("/api/v2/alignment-matrix").json() == alignment.json()
        assert client.get("/api/v2/phase-transition").json() == phase.json()
