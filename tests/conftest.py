import pytest

from leadorbyt import store


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Give each test a fresh SQLite database instead of sharing the real leadorbyt.db."""
    monkeypatch.setattr(store, "_connection", None)
    store.connect(tmp_path / "test.db")
    yield
    if store._connection is not None:
        store._connection.close()
    store._connection = None
