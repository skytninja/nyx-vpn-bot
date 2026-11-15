"""Durable orders. Every mutation is serialized by the per-user advisory lock."""

from contextlib import closing, contextmanager

from psycopg2.extras import RealDictCursor

from app.storage import _connect


@contextmanager
def user_lock(tg_id):
    conn = _connect()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_lock(7742, hashtext(%s))", (str(tg_id),)
            )
            acquired = cur.fetchone()[0]
        if not acquired:
            raise RuntimeError("Операция уже выполняется. Повторите чуть позже.")
        yield
    finally:
        conn.close()  # Session lock is released even on failure.


def query(sql, params=(), one=True):
    with closing(_connect()) as conn, conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            if cur.description:
                return cur.fetchone() if one else cur.fetchall()


def binding(tg_id):
    return query("SELECT * FROM remna_accounts WHERE tg_id=%s", (tg_id,))


def bind(tg_id, user):
    existing = binding(tg_id)
    if existing and existing["remna_id"] != user["id"]:
        raise RuntimeError("Account binding mismatch")
    query(
        """INSERT INTO remna_accounts(tg_id,remna_id,short_uuid)
        VALUES(%s,%s,%s) ON CONFLICT(tg_id) DO UPDATE
        SET short_uuid=EXCLUDED.short_uuid, updated_at=NOW()""",
        (tg_id, user["id"], user["shortUuid"]),
    )


def order(order_id):
    return query("SELECT * FROM vpn_orders WHERE id=%s", (order_id,))


def open_order(tg_id):
    return query(
        "SELECT * FROM vpn_orders WHERE tg_id=%s AND status IN ('draft','submitted','applying')",
        (tg_id,),
    )


def create_order(tg_id, months, days, amount, kind="payment"):
    return query(
        """INSERT INTO vpn_orders(tg_id,months,days,amount,kind)
        VALUES(%s,%s,%s,%s,%s) RETURNING *""",
        (tg_id, months, days, amount, kind),
    )


def submit_receipt(tg_id, chat_id, message_id):
    return query(
        """UPDATE vpn_orders SET status='submitted', receipt_chat_id=%s,
        receipt_message_id=%s, updated_at=NOW() WHERE tg_id=%s AND status='draft'
        RETURNING *""",
        (chat_id, message_id, tg_id),
    )


def prepare(order_id, user_id, end_at):
    return query(
        """UPDATE vpn_orders SET status='applying',remna_id=%s,target_end=%s,
        updated_at=NOW() WHERE id=%s AND status IN ('draft','submitted','applying') RETURNING *""",
        (user_id, end_at, order_id),
    )


def finish(order_id):
    query(
        "UPDATE vpn_orders SET status='approved',updated_at=NOW() WHERE id=%s AND status='applying'",
        (order_id,),
    )
