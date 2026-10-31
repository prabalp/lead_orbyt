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
