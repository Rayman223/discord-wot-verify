import pytest

from wot_verify.store import Store


def test_unique_contact_survives_restart_and_can_be_released(tmp_path):
    path = str(tmp_path / "contacts.sqlite3")
    first = Store(path)
    second = Store(path)
    try:
        first.link(1, "eu", 10, "Player")
        first.reserve_contact(1, "eu", 42)
        with pytest.raises(ValueError, match="clan_taken"):
            second.reserve_contact(2, "eu", 42)
        with pytest.raises(ValueError, match="clan_taken"):
            second.reserve_contact(1, "eu", 43)
        second.reserve_contact(1, "eu", 42)  # Safe retry of the same reservation.
        first.close()
        first = Store(path)
        assert first.clan_contact("eu", 42)["discord_id"] == 1
        first.unlink(1)
        second.reserve_contact(2, "eu", 42)
        assert second.contact(2)["clan_id"] == 42
    finally:
        first.close()
        second.close()
