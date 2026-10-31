from leadorbyt import auth, store


def test_disabling_a_user_blocks_their_keys_and_enabling_restores_them(isolated_db):
    user_id = store.create_user("owner@example.com")
    key_hash = auth.hash_key(store.create_api_key(user_id))
    assert store.get_user_for_key(key_hash) == user_id

    assert store.set_user_disabled(user_id, True) is True
    assert store.get_user_for_key(key_hash) is None

    assert store.set_user_disabled(user_id, False) is True
    assert store.get_user_for_key(key_hash) == user_id


def test_unknown_user_returns_false(isolated_db):
    assert store.set_user_disabled("does-not-exist", True) is False


def test_extra_labelled_keys_list_and_revoke_by_id(isolated_db):
    user_id = store.create_user("owner@example.com")
    first = store.create_api_key(user_id)
    second = store.create_api_key(user_id, label="orbytlink")

    keys = store.list_api_keys(user_id)
    assert [k["label"] for k in keys] == [None, "orbytlink"]
    assert all(len(k["id"]) == store.KEY_ID_LENGTH for k in keys)

    assert store.revoke_api_key_by_id(keys[1]["id"]) is True
    assert store.get_user_for_key(auth.hash_key(second)) is None
    assert store.get_user_for_key(auth.hash_key(first)) == user_id  # other key unaffected
    assert store.revoke_api_key_by_id(keys[1]["id"]) is False  # already revoked


def test_revoke_by_id_rejects_short_or_wildcard_ids(isolated_db):
    user_id = store.create_user("owner@example.com")
    store.create_api_key(user_id)
    assert store.revoke_api_key_by_id("abc") is False
    assert store.revoke_api_key_by_id("%" * store.KEY_ID_LENGTH) is False
    assert store.revoke_api_key_by_id("____________") is False


def test_old_db_gets_label_column(tmp_path, monkeypatch):
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE api_keys (key_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at REAL NOT NULL, revoked_at REAL)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "_connection", None)
    store.connect(db)
    try:
        cols = {row[1] for row in store._connection.execute("PRAGMA table_info(api_keys)").fetchall()}
        assert "label" in cols
    finally:
        store._connection.close()
        store._connection = None
