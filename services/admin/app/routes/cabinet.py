from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import jwt
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from psycopg2.extras import RealDictCursor

from app.config import get_bot_username
from app.db import _connect, get_subscription, get_user
from app.services.remnawave import RemnawaveClient, is_unlimited, parse_date

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[2]
CABINET_DIR = BASE_DIR / "static" / "cabinet"
router = APIRouter(prefix="/cabinet", tags=["cabinet"])


def _decode_token(token: str) -> int:
    """Декодировать JWT и вернуть tg_id. Raises HTTPException если невалидный."""
    secret = os.getenv("JWT_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="JWT_SECRET not configured")
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        tg_id = int(payload.get("sub", 0))
        if tg_id <= 0:
            raise ValueError("Invalid tg_id")
        return tg_id
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except (jwt.InvalidTokenError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid token")


@router.get("")
async def cabinet_page(t: str = Query(...)):
    """HTML-страница личного кабинета (React SPA, собирается из nyx_vpn_index_web)."""
    _decode_token(t)
    index = CABINET_DIR / "index.html"
    if not index.exists():
        raise HTTPException(
            status_code=500,
            detail="Cabinet SPA not built. Run: cd nyx_vpn_index_web && npm install && npm run build",
        )
    return FileResponse(str(index))


@router.get("/api/subscription")
async def cabinet_api(t: str = Query(...)):
    """API кабинета: срок и доступ читаются из Remnawave по проверенной связи."""
    tg_id = _decode_token(t)

    user = get_user(tg_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    sub = get_subscription(tg_id)

    now = datetime.now(timezone.utc)
    is_active = False
    days_left = None
    reason = "unmapped" if sub else None
    unlimited = False
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT remna_id FROM remna_accounts WHERE tg_id=%s", (tg_id,))
            account = cur.fetchone()
    if account:
        client = RemnawaveClient()
        try:
            live = await client.get_user(account["remna_id"])
        except Exception:
            raise HTTPException(
                status_code=503, detail="Subscription status temporarily unavailable"
            )
        finally:
            await client.close()
        end_at = parse_date(live["expireAt"])
        unlimited = is_unlimited(live)
        is_active = live["status"] == "ACTIVE" and end_at > now
        reason = None if is_active else live["status"].lower()
        days_left = None if unlimited or not is_active else (end_at - now).days
        sub = dict(sub or {})
        sub["end_at"] = end_at
        sub["subscription_link"] = client.subscription_link(live) if is_active else None
        sub["instructions"] = None
        # Cabinet always reads the current panel date; DB is a cache for the bot.

    response = {
        "username": user.get("username") or f"tg_{tg_id}",
        "tg_id": tg_id,
        "is_active": is_active,
        "reason": reason,
        "subscription": None,
        "referral_balance": int(user.get("referral_balance") or 0),
        "bot_username": get_bot_username(),
        "payment_amount": int(os.getenv("PAYMENT_AMOUNT", "150")),
        "payment_days": int(os.getenv("PAYMENT_DAYS", "30")),
    }

    if sub:
        # Панель-ссылка не отдаётся как действующая, если клиент удалён.
        link = sub.get("subscription_link") if is_active else None
        response["subscription"] = {
            "start_at": sub["start_at"].isoformat() if sub.get("start_at") else None,
            "end_at": sub["end_at"].isoformat() if sub.get("end_at") else None,
            "days_left": days_left,
            "unlimited": unlimited,
            "subscription_link": link,
            "instructions": sub.get("instructions"),
        }

    return JSONResponse(response)
