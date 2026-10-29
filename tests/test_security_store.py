import sqlite3
from unittest.mock import patch

import pytest

from wot_verify.security import States
from wot_verify.store import Store


@pytest.fixture
def store(tmp_path):
    db = Store(str(tmp_path / "test.sqlite3"))
    yield db
    db.close()


def test_one_use_and_tampering(store):
    states = States("x" * 32, 123, store)
    token = states.issue(456)
    with pytest.raises(ValueError):
        states.consume(token.replace("456", "789"))
    assert states.consume(token) == 456
    with pytest.raises(ValueError):
        states.consume(token)


def test_expiry_new_link_and_server_binding(store):
    states = States("x" * 32, 123, store)
    token = states.issue(456)
    with pytest.raises(ValueError):
        States("x" * 32, 999, store).consume(token)
    with patch("wot_verify.security.time.time", return_value=2**40), pytest.raises(ValueError):
        states.consume(token)
    new = states.issue(456)
    with pytest.raises(ValueError):
        states.consume(token)
    assert states.consume(new) == 456


def test_unique_both_sides_unlink_invalidates_challenges(store):
    store.link(1, "eu", 10, "Player")
    store.link(1, "eu", 10, "Player")  # Safe retry of the same identity.
    for args in [(2, "eu", 10, "Player"), (1, "eu", 20, "Other")]:
        with pytest.raises(ValueError, match="already_linked"):
            store.link(*args)
    states = States("x" * 32, 123, store)
    token = states.issue(1)
    store.unlink(1)
    with pytest.raises(ValueError):
        states.consume(token)
    store.link(2, "eu", 10, "Player")
    assert store.by_account("eu", 10)["discord_id"] == 2


def test_migrations_restart_and_monthly_due(tmp_path):
    path = str(tmp_path / "test.sqlite3")
    db = Store(path)
    db.link(1, "eu", 10, "Player")
    assert len(db.due()) == 1
    db.mark_synced(1, "NewName")
    assert not db.due()
    db.close()
    db = Store(path)
    assert db.get(1)["nickname"] == "NewName"
    assert db.db.execute("SELECT count(*) FROM migrations").fetchone()[0] == 8
    with pytest.raises(sqlite3.IntegrityError):
        db.db.execute("INSERT INTO links VALUES (2, 'eu', 10, 'Duplicate', 0)")
    db.close()


def test_signed_channel_and_legacy_links(store):
    states = States("x" * 32, 123, store)
    token = states.issue(456, 789)
    assert states.consume_details(token) == (456, 789)
    token = states.issue(456)
    payload = token.rsplit(".", 2)[0]  # Simulate a link issued before channel context existed.
    legacy = f"{payload}.{states.signature(payload)}"
    assert states.consume_details(legacy) == (456, 0)


def test_upgrade_existing_database_preserves_links_and_is_idempotent(tmp_path):
    from pathlib import Path

    import wot_verify

    path = str(tmp_path / "upgrade.sqlite3")
    connection = sqlite3.connect(path)
    root = Path(wot_verify.__file__).parent / "migrations"
    connection.execute("CREATE TABLE migrations (name TEXT PRIMARY KEY)")
    for migration in sorted(root.glob("00[1-4]_*.sql")):
        connection.executescript(migration.read_text())
        connection.execute("INSERT INTO migrations VALUES (?)", (migration.name,))
    connection.execute("INSERT INTO links VALUES (7, 'eu', 100, 'Player', 123)")
    connection.commit()
    connection.close()
    for _ in range(2):
        db = Store(path)
        assert db.get(7)["synced_at"] == 123
        assert db.db.execute("SELECT count(*) FROM migrations").fetchone()[0] == 8
        db.close()


def test_retry_hashes_and_bindings_survive_restart(tmp_path):
    path = str(tmp_path / "retry.sqlite3")
    db = Store(path)
    db.link(7, "eu", 100, "Player")
    cookie, csrf, lifetime = db.issue_retry(7, "eu", 100, 42)
    assert 0 < lifetime <= 600
    saved = dict(db.db.execute("SELECT * FROM retry_sessions").fetchone())
    assert cookie not in str(saved)
    assert csrf not in str(saved)
    db.close()
    db = Store(path)
    row = db.consume_retry(cookie, csrf)
    assert (row["discord_id"], row["account_id"], row["channel_id"]) == (7, 100, 42)
    with pytest.raises(ValueError, match="invalid_state"):
        db.consume_retry(cookie, csrf)
    db.close()
