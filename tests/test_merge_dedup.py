from leadorbyt.merge import CSV_FIELDS, dedup_key, merge_records


def _item(**overrides):
    base = {
        "business_name": "Acme Coffee",
        "category": "Coffee shop",
        "website": "https://acme-coffee.com",
        "phone": "555-1234",
        "address": "1 Main St",
        "discovered_by_query": "coffee shops | Austin, TX",
    }
    base.update(overrides)
    return base


def test_dedup_key_uses_domain_when_website_present():
    assert dedup_key(_item()) == "acme-coffee.com"


def test_dedup_key_falls_back_to_name_and_address_when_no_website():
    item = _item(website="")
    key = dedup_key(item)
    assert key == "name:acme coffee|1 main st"


def test_two_no_website_businesses_with_different_names_do_not_collide():
    a = dedup_key(_item(website="", business_name="Acme Coffee"))
    b = dedup_key(_item(website="", business_name="Beta Coffee"))
    assert a != b


def test_merge_records_dedupes_same_domain():
    items = [_item(), _item()]
    merged = merge_records(items, {})
    assert len(merged) == 1


def test_merge_records_dedupes_no_website_by_name_and_address():
    items = [_item(website=""), _item(website="")]
    merged = merge_records(items, {})
    assert len(merged) == 1


def test_merge_records_keeps_distinct_no_website_businesses():
    items = [
        _item(website="", business_name="Acme Coffee"),
        _item(website="", business_name="Beta Coffee", address="2 Side St"),
    ]
    merged = merge_records(items, {})
    assert len(merged) == 2


def test_merge_records_carries_provenance_field():
    merged = merge_records([_item()], {})
    assert merged[0]["discovered_by_query"] == "coffee shops | Austin, TX"


def test_csv_fields_include_new_columns():
    for column in ("discovered_by_query", "is_new_lead", "qualified", "qualification_score", "qualification_reason"):
        assert column in CSV_FIELDS
