import time

import pytest

from leadorbyt import cleanup, config


@pytest.fixture
def output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULT_RETENTION_DAYS", 7)
    return tmp_path


def _write_csv(path, age_days, now):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("a,b\n1,2\n")
    mtime = now - age_days * 86400
    import os

    os.utime(path, (mtime, mtime))


def test_purge_deletes_only_files_older_than_retention(output_dir):
    now = time.time()
    old = output_dir / "user1" / "old.csv"
    fresh = output_dir / "user1" / "fresh.csv"
    _write_csv(old, age_days=10, now=now)
    _write_csv(fresh, age_days=1, now=now)

    deleted = cleanup.purge_old_result_files(now=now)

    assert deleted == 1
    assert not old.exists()
    assert fresh.exists()


def test_purge_across_multiple_tenants(output_dir):
    now = time.time()
    old1 = output_dir / "user1" / "old.csv"
    old2 = output_dir / "user2" / "old.csv"
    fresh = output_dir / "user2" / "fresh.csv"
    _write_csv(old1, age_days=8, now=now)
    _write_csv(old2, age_days=30, now=now)
    _write_csv(fresh, age_days=0, now=now)

    deleted = cleanup.purge_old_result_files(now=now)

    assert deleted == 2
    assert not old1.exists()
    assert not old2.exists()
    assert fresh.exists()


def test_purge_ignores_non_csv_files(output_dir):
    now = time.time()
    old_txt = output_dir / "user1" / "old.txt"
    old_txt.parent.mkdir(parents=True, exist_ok=True)
    old_txt.write_text("not a csv")
    import os

    os.utime(old_txt, (now - 30 * 86400, now - 30 * 86400))

    deleted = cleanup.purge_old_result_files(now=now)

    assert deleted == 0
    assert old_txt.exists()


def test_purge_missing_output_dir_returns_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "does-not-exist")
    assert cleanup.purge_old_result_files() == 0


def test_purge_exactly_at_retention_boundary_is_kept(output_dir):
    now = time.time()
    boundary = output_dir / "user1" / "boundary.csv"
    # Slightly younger than the cutoff -- must survive.
    _write_csv(boundary, age_days=6.99, now=now)

    deleted = cleanup.purge_old_result_files(now=now)

    assert deleted == 0
    assert boundary.exists()
