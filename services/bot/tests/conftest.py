import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(scope="session")
def database(tmp_path_factory):
    import os

    import pgserver
    from app import config
    from app.storage import init_db

    server = pgserver.get_server(
        tmp_path_factory.mktemp("remna-postgres"), cleanup_mode="delete"
    )
    os.environ["DATABASE_URL"] = server.get_uri()
    os.environ["REMNA_SUB_URL"] = "https://sub.example.test"
    config._ENV_LOADED = True
    init_db()
    yield
    server.cleanup()


@pytest.fixture(autouse=True)
def clean_db(database, monkeypatch):
    from app import order_storage as orders
    from app import storage

    orders.query(
        "TRUNCATE vpn_orders, remna_accounts, subscriptions, pending_subscriptions, users RESTART IDENTITY CASCADE"
    )
    monkeypatch.setattr(storage, "_cache_get_subscription", lambda *a: None)
    monkeypatch.setattr(storage, "_cache_set_subscription", lambda *a, **k: None)
    monkeypatch.setattr(storage, "_cache_clear_subscription", lambda *a: None)
