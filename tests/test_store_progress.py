from leadorbyt import store


def test_upsert_lead_returns_true_on_first_sight(isolated_db):
    is_new = store.upsert_lead("user1", "acme.com", "Acme Coffee", "acme.com", "coffee shops", "austin, tx", "q")
    assert is_new is True


def test_upsert_lead_returns_false_on_repeat(isolated_db):
    store.upsert_lead("user1", "acme.com", "Acme Coffee", "acme.com", "coffee shops", "austin, tx", "q")
    is_new = store.upsert_lead("user1", "acme.com", "Acme Coffee", "acme.com", "coffee shops", "austin, tx", "q")
    assert is_new is False


def test_upsert_lead_scoped_per_user(isolated_db):
    store.upsert_lead("user1", "acme.com", "Acme Coffee", "acme.com", "coffee shops", "austin, tx", "q")
    is_new_for_other_user = store.upsert_lead(
        "user2", "acme.com", "Acme Coffee", "acme.com", "coffee shops", "austin, tx", "q"
    )
    assert is_new_for_other_user is True


def test_update_job_progress_round_trips_via_get_job(isolated_db):
    store.create_job("job1", "user1", "coffee shops", "austin, tx", 10)
    store.update_job_progress("job1", stage="enriching", items_discovered=5, items_total=10)
    job = store.get_job("job1", "user1")
    assert job["stage"] == "enriching"
    assert job["items_discovered"] == 5
    assert job["items_total"] == 10
    assert job["items_enriched"] is None  # untouched field stays None


def test_update_job_progress_patches_incrementally(isolated_db):
    store.create_job("job1", "user1", "coffee shops", "austin, tx", 10)
    store.update_job_progress("job1", stage="enriching")
    store.update_job_progress("job1", items_enriched=3)
    job = store.get_job("job1", "user1")
    assert job["stage"] == "enriching"
    assert job["items_enriched"] == 3


def test_fail_job_records_error_type(isolated_db):
    store.create_job("job1", "user1", "coffee shops", "austin, tx", 10)
    store.fail_job("job1", "blocked by captcha", "blocked")
    job = store.get_job("job1", "user1")
    assert job["status"] == "error"
    assert job["error_type"] == "blocked"
