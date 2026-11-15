import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.routes import cabinet


@pytest.fixture
def endpoint(monkeypatch):
    monkeypatch.setattr(cabinet, "_decode_token", lambda t: 100)
    monkeypatch.setattr(cabinet, "get_user", lambda tg: {"username": "test"})
    monkeypatch.setattr(
        cabinet,
        "get_subscription",
        lambda tg: {"end_at": datetime(2020, 1, 1, tzinfo=timezone.utc)},
    )
    connection = MagicMock()
    connection.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchone.return_value = {
        "remna_id": 41
    }
    monkeypatch.setattr(cabinet, "_connect", lambda: connection)
    monkeypatch.setattr(cabinet, "get_bot_username", lambda: "testbot")

    class Client:
        user = {
            "id": 41,
            "shortUuid": "test",
            "status": "ACTIVE",
            "expireAt": (datetime.now(timezone.utc) + timedelta(days=10)).isoformat(),
        }

        async def get_user(self, id):
            return self.user

        async def close(self):
            pass

        def subscription_link(self, user):
            return "https://sub.test/test"

    monkeypatch.setattr(cabinet, "RemnawaveClient", Client)
    return Client


@pytest.mark.asyncio
async def test_live_panel_date_overrides_stale_database(endpoint):
    import json

    data = json.loads((await cabinet.cabinet_api("token")).body)
    assert data["is_active"] is True
    assert data["subscription"]["end_at"] == endpoint.user["expireAt"]


@pytest.mark.asyncio
async def test_disabled_panel_account_hides_link(endpoint):
    import json

    endpoint.user["status"] = "DISABLED"
    data = json.loads((await cabinet.cabinet_api("token")).body)
    assert data["is_active"] is False
    assert data["subscription"]["subscription_link"] is None


@pytest.mark.asyncio
async def test_unlimited_flag(endpoint):
    import json

    endpoint.user["expireAt"] = "9999-12-31T00:00:00+00:00"
    data = json.loads((await cabinet.cabinet_api("token")).body)
    assert data["is_active"] and data["subscription"]["unlimited"]
    assert data["subscription"]["days_left"] is None


@pytest.mark.asyncio
async def test_panel_outage_does_not_report_stale_active_access(endpoint):
    from fastapi import HTTPException

    async def unavailable(self, id):
        raise TimeoutError()

    endpoint.get_user = unavailable
    with pytest.raises(HTTPException) as error:
        await cabinet.cabinet_api("token")
    assert error.value.status_code == 503
