import pytest

from leadorbyt import auth, config


def _write_csv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(header)] + [",".join(row) for row in rows]
    path.write_text("\n".join(lines) + ("\n" if lines else ""))


@pytest.fixture
def user_output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(config, "PUBLIC_URL", "https://lead.orbyt.in")
    return tmp_path / "user1"


async def _call_as(user_id, coro_factory):
    token = auth.current_user_id.set(user_id)
    try:
        return await coro_factory()
    finally:
        auth.current_user_id.reset(token)


@pytest.mark.asyncio
async def test_read_result_csv_paginates(user_output_dir):
    from leadorbyt import files, server

    csv_path = user_output_dir / "leads.csv"
    _write_csv(csv_path, ["name", "email"], [[f"person{i}", f"p{i}@x.com"] for i in range(5)])
    url = files.download_url(str(csv_path))

    result = await _call_as("user1", lambda: server.read_result_csv(url, offset=1, limit=2))

    assert result["total_rows"] == 5
    assert result["offset"] == 1
    assert result["limit"] == 2
    assert result["columns"] == ["name", "email"]
    assert [r["name"] for r in result["rows"]] == ["person1", "person2"]


@pytest.mark.asyncio
async def test_read_result_csv_caps_limit_server_side(user_output_dir):
    from leadorbyt import files, server

    csv_path = user_output_dir / "leads.csv"
    _write_csv(csv_path, ["name"], [[f"person{i}"] for i in range(300)])
    url = files.download_url(str(csv_path))

    result = await _call_as("user1", lambda: server.read_result_csv(url, offset=0, limit=10_000))

    assert result["limit"] == server.RESULT_CSV_MAX_LIMIT
    assert len(result["rows"]) == server.RESULT_CSV_MAX_LIMIT
    assert result["total_rows"] == 300


@pytest.mark.asyncio
async def test_read_result_csv_rejects_path_outside_tenant_dir(user_output_dir, tmp_path):
    from leadorbyt import server

    outside = tmp_path / "user2" / "secret.csv"
    _write_csv(outside, ["name"], [["hacker"]])

    with pytest.raises(RuntimeError, match="invalid_input"):
        await _call_as("user1", lambda: server.read_result_csv(str(outside)))


@pytest.mark.asyncio
async def test_read_result_csv_rejects_traversal_attempt(user_output_dir, tmp_path):
    from leadorbyt import server

    other_tenant_secret = tmp_path / "user2" / "secret.csv"
    _write_csv(other_tenant_secret, ["name"], [["hacker"]])
    # Traversal starting from user1's own directory reaching into user2's.
    traversal_path = str(user_output_dir / ".." / "user2" / "secret.csv")

    with pytest.raises(RuntimeError, match="invalid_input"):
        await _call_as("user1", lambda: server.read_result_csv(traversal_path))


@pytest.mark.asyncio
async def test_read_result_csv_nonexistent_path(user_output_dir):
    from leadorbyt import server

    missing = user_output_dir / "never_created.csv"

    with pytest.raises(RuntimeError, match="not_found"):
        await _call_as("user1", lambda: server.read_result_csv(str(missing)))


@pytest.mark.asyncio
async def test_read_result_csv_empty_csv(user_output_dir):
    from leadorbyt import files, server

    csv_path = user_output_dir / "empty.csv"
    _write_csv(csv_path, ["name", "email"], [])
    url = files.download_url(str(csv_path))

    result = await _call_as("user1", lambda: server.read_result_csv(url))

    assert result["total_rows"] == 0
    assert result["rows"] == []
    assert result["columns"] == ["name", "email"]


@pytest.mark.asyncio
async def test_list_result_files_filters_by_prefix(user_output_dir):
    from leadorbyt import server

    _write_csv(user_output_dir / "web_signals_crm.csv", ["name"], [["a"]])
    _write_csv(user_output_dir / "people_ciso.csv", ["name"], [["b"]])

    result = await _call_as("user1", lambda: server.list_result_files(prefix="web_signals"))

    assert [f["name"] for f in result["files"]] == ["web_signals_crm.csv"]
    assert result["files"][0]["result_path"].startswith("https://lead.orbyt.in/files/user1/")


@pytest.mark.asyncio
async def test_list_result_files_empty_for_unknown_tenant(tmp_path, monkeypatch):
    from leadorbyt import server

    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)

    result = await _call_as("brand-new-user", lambda: server.list_result_files())

    assert result["files"] == []
