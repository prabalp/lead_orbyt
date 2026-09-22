import pytest

from leadorbyt import config, files
from leadorbyt.errors import LeadOrbytError


@pytest.fixture
def output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(config, "PUBLIC_URL", "https://lead.orbyt.in")
    return tmp_path


def test_download_url_none_for_falsy_path(output_dir):
    assert files.download_url(None) is None
    assert files.download_url("") is None


def test_download_url_builds_url_under_public_host(output_dir):
    csv_path = output_dir / "user1" / "leads.csv"
    csv_path.parent.mkdir(parents=True)
    csv_path.write_text("a,b\n1,2\n")

    url = files.download_url(str(csv_path))

    assert url == "https://lead.orbyt.in/files/user1/leads.csv"


def test_download_url_rejects_path_outside_output_dir(output_dir, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "leads.csv"
    outside.write_text("a,b\n")

    with pytest.raises(LeadOrbytError):
        files.download_url(str(outside))


def test_resolve_owned_path_round_trips_through_download_url(output_dir):
    csv_path = output_dir / "user1" / "leads.csv"
    csv_path.parent.mkdir(parents=True)
    csv_path.write_text("a,b\n1,2\n")

    url = files.download_url(str(csv_path))
    resolved = files.resolve_owned_path(url, "user1")

    assert resolved == str(csv_path.resolve())


def test_resolve_owned_path_rejects_other_users_file(output_dir):
    csv_path = output_dir / "user1" / "leads.csv"
    csv_path.parent.mkdir(parents=True)
    csv_path.write_text("a,b\n1,2\n")

    url = files.download_url(str(csv_path))

    with pytest.raises(LeadOrbytError):
        files.resolve_owned_path(url, "user2")


def test_resolve_owned_path_rejects_nonexistent_file(output_dir):
    with pytest.raises(LeadOrbytError):
        files.resolve_owned_path(f"{config.PUBLIC_URL}/files/user1/missing.csv", "user1")


def test_resolve_owned_path_accepts_raw_local_path(output_dir):
    csv_path = output_dir / "user1" / "leads.csv"
    csv_path.parent.mkdir(parents=True)
    csv_path.write_text("a,b\n1,2\n")

    resolved = files.resolve_owned_path(str(csv_path), "user1")

    assert resolved == str(csv_path.resolve())
