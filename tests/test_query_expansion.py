from leadorbyt import query_expansion, store


def test_extract_candidate_tokens_filters_stopwords_and_short_words():
    person = {"title": "VP of Security and IT Operations"}
    tokens = query_expansion.extract_candidate_tokens(person)
    assert "security" in tokens
    assert "operations" in tokens
    assert "and" not in tokens
    assert "of" not in tokens
    assert "it" not in tokens  # shorter than the 3-char minimum


def test_record_outcome_bumps_accept_and_reject_counts(isolated_db):
    query_expansion.record_outcome("user1", "icphash", {"title": "Security Director"}, qualified=True)
    query_expansion.record_outcome("user1", "icphash", {"title": "Security Analyst"}, qualified=False)

    rows = {token: (accept, reject) for token, field, accept, reject in store.get_frontier_tokens("user1", "icphash")}
    assert rows["security"] == (1, 1)  # appeared in both an accept and a reject
    assert rows["director"] == (1, 0)
    assert rows["analyst"] == (0, 1)


def test_sample_next_titles_excludes_existing_titles(isolated_db):
    store.bump_frontier_token("user1", "icphash", "director", "title", accepted=True)
    store.bump_frontier_token("user1", "icphash", "manager", "title", accepted=True)

    sampled = query_expansion.sample_next_titles("user1", "icphash", ["Director"], n=5)
    assert "director" not in sampled
    assert "manager" in sampled


def test_sample_next_titles_returns_empty_when_no_frontier(isolated_db):
    assert query_expansion.sample_next_titles("user1", "icphash", [], n=3) == []


def test_sample_next_titles_respects_n(isolated_db):
    for token in ["alpha", "beta", "gamma", "delta"]:
        store.bump_frontier_token("user1", "icphash", token, "title", accepted=True)

    sampled = query_expansion.sample_next_titles("user1", "icphash", [], n=2)
    assert len(sampled) == 2


def test_frontier_scoped_per_user_and_icp(isolated_db):
    store.bump_frontier_token("user1", "icp_a", "director", "title", accepted=True)
    assert store.get_frontier_tokens("user1", "icp_b") == []
    assert store.get_frontier_tokens("user2", "icp_a") == []
