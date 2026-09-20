from leadorbyt.signal_merge import SIGNAL_CSV_FIELDS, dedup_key


def test_dedup_key_uses_post_id():
    signal = {"reddit_post_id": "abc123", "reddit_username": "u1"}
    assert dedup_key(signal) == "reddit:abc123"


def test_dedup_key_handles_missing_post_id():
    assert dedup_key({}) == "reddit:"


def test_signal_csv_fields_shape():
    for column in (
        "reddit_username", "subreddit", "post_title", "post_body", "permalink",
        "qualified", "qualification_reason", "discovered_by_query", "is_new_lead",
    ):
        assert column in SIGNAL_CSV_FIELDS
