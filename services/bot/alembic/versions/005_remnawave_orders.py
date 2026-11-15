"""Durable Remnawave bindings and payment operations."""

from alembic import op

revision = "005_remnawave_orders"
down_revision = "004_pending_subscriptions"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE remna_accounts (
        tg_id BIGINT PRIMARY KEY REFERENCES users(tg_id),
        remna_id BIGINT UNIQUE NOT NULL,
        short_uuid TEXT UNIQUE NOT NULL,
        source_sub_id TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
    op.execute("""CREATE TABLE vpn_orders (
        id BIGSERIAL PRIMARY KEY,
        tg_id BIGINT NOT NULL REFERENCES users(tg_id),
        kind TEXT NOT NULL CHECK(kind IN ('payment', 'trial')),
        months INTEGER NOT NULL DEFAULT 0,
        days INTEGER NOT NULL CHECK(days > 0),
        amount INTEGER NOT NULL CHECK(amount >= 0),
        status TEXT NOT NULL DEFAULT 'draft'
            CHECK(status IN ('draft','submitted','applying','approved','rejected')),
        receipt_chat_id BIGINT, receipt_message_id BIGINT,
        remna_id BIGINT, target_end TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(receipt_chat_id, receipt_message_id))""")
    op.execute(
        "CREATE UNIQUE INDEX vpn_one_open_order ON vpn_orders(tg_id) WHERE status IN ('draft','submitted','applying')"
    )
    op.execute(
        "CREATE UNIQUE INDEX vpn_one_trial ON vpn_orders(tg_id) WHERE kind = 'trial'"
    )


def downgrade():
    op.drop_table("vpn_orders")
    op.drop_table("remna_accounts")
