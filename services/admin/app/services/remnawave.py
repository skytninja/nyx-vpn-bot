"""Client for the installed Remnawave API (numeric user IDs)."""

import os
from datetime import datetime, timezone

import httpx


def parse_date(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def is_unlimited(user):
    return parse_date(user["expireAt"]).year >= 9999


class RemnawaveClient:
    def __init__(self):
        self.http = httpx.AsyncClient(
            base_url=os.environ["REMNA_URL"].rstrip("/") + "/api/",
            headers={"Authorization": "Bearer " + os.environ["REMNA_TOKEN"]},
            timeout=25.0,
        )

    async def close(self):
        await self.http.aclose()

    async def request(self, method, path, body=None):
        response = await self.http.request(method, path, json=body)
        # Do not log response bodies, URLs or authorization credentials.
        if response.status_code >= 400:
            raise RuntimeError(f"Remnawave API returned HTTP {response.status_code}")
        return response.json()["response"]

    async def get_user(self, user_id):
        return await self.request("GET", f"users/{int(user_id)}")

    async def find_bot_user(self, tg_id):
        response = await self.http.get(f"users/by-username/tg_{int(tg_id)}")
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise RuntimeError(f"Remnawave lookup HTTP {response.status_code}")
        user = response.json()["response"]
        if user.get("telegramId") != tg_id:
            raise RuntimeError("Account ownership mismatch")
        return user

    async def create_user(self, tg_id, end_at):
        return await self.request(
            "POST",
            "users",
            {
                "username": f"tg_{int(tg_id)}",
                "telegramId": tg_id,
                "expireAt": end_at.isoformat(),
                "trafficLimitBytes": 0,
                "trafficLimitStrategy": "NO_RESET",
                "hwidDeviceLimit": 0,
                "activeInternalSquads": [os.environ["REMNA_SQUAD_ID"]],
                "status": "ACTIVE",
            },
        )

    async def set_expiry(self, user_id, end_at):
        return await self.request(
            "PATCH",
            "users",
            {
                "id": int(user_id),
                "expireAt": end_at.isoformat(),
                "status": "ACTIVE",
            },
        )

    def subscription_link(self, user):
        return os.environ["REMNA_SUB_URL"].rstrip("/") + "/" + user["shortUuid"]
