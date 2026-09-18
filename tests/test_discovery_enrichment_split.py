import csv
import inspect

from leadorbyt import jobs, server
from leadorbyt.merge import CSV_FIELDS, export_csv, merge_records


def _discovery_row():
    return {
        "business_name": "Acme Coffee",
        "category": "Coffee shop",
        "website": "https://acme.example",
        "phone": "555-0100",
        "address": "1 Main St",
        "lat": 30.1,
        "lon": -97.7,
        "discovered_by_query": "coffee shops | Austin, TX",
    }


def test_discovery_csv_has_research_fields_and_blank_enrichment(tmp_path):
    rows = merge_records([_discovery_row()], {}, {})
    path = export_csv(rows, tmp_path / "discovered.csv")

    with path.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))

    assert row["business_name"] == "Acme Coffee"
    assert row["phone"] == "555-0100"
    assert row["lat"] == "30.1"
    assert row["email"] == ""
    assert row["extra_data_json"] == ""


async def test_enrich_lead_list_enriches_existing_csv_without_discovery(tmp_path, monkeypatch):
    source_rows = merge_records([_discovery_row()], {}, {})
    source_rows[0]["is_new_lead"] = True
    source = export_csv(source_rows, tmp_path / "discovered.csv")

    async def fake_enrich_all(websites, job=None):
        assert websites == ["https://acme.example"]
        return {"acme.example": {"email": "hello@acme.example"}}

    async def fake_extras(items, location):
        assert location == "Austin, TX"
        return {"acme.example": {"sources_used": ["example"], "example_score": 10}}

    monkeypatch.setattr(jobs, "_enrich_all", fake_enrich_all)
    monkeypatch.setattr(jobs, "_enrich_extras_all", fake_extras)

    result = await jobs.enrich_lead_list(str(source), "user-1")
    with open(result, newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))

    assert row["email"] == "hello@acme.example"
    assert row["extra_sources_used"] == "example"
    assert row["is_new_lead"] == "True"


def test_people_search_defaults_to_no_paid_enrichment():
    assert inspect.signature(server.find_people_leads).parameters["max_paid_lookups"].default == 0
    assert inspect.signature(server.submit_people_search).parameters["max_paid_lookups"].default == 0


def test_csv_fields_include_coordinates_for_later_enrichment():
    assert "lat" in CSV_FIELDS
    assert "lon" in CSV_FIELDS
