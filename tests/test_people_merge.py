from leadorbyt.people_merge import PEOPLE_CSV_FIELDS, company_domain_key, dedup_key


def test_dedup_key_uses_apollo_id_when_present():
    person = {"apollo_person_id": "p1", "full_name": "Jane Smith", "company_name": "Acme"}
    assert dedup_key(person) == "apollo:p1"


def test_dedup_key_uses_bettercontact_id_when_present():
    person = {"bc_lead_id": "bc1", "full_name": "Jane Smith", "company_name": "Acme"}
    assert dedup_key(person) == "bettercontact:bc1"


def test_dedup_key_falls_back_to_linkedin_url_when_no_provider_id():
    person = {"linkedin_url": "https://linkedin.com/in/janesmith", "full_name": "Jane Smith"}
    assert dedup_key(person) == "linkedin:https://linkedin.com/in/janesmith"


def test_dedup_key_falls_back_to_name_and_company():
    person = {"apollo_person_id": "", "full_name": "Jane Smith", "company_name": "Acme Corp"}
    assert dedup_key(person) == "name:jane smith|acme corp"


def test_dedup_key_distinguishes_same_name_different_company():
    a = dedup_key({"full_name": "Jane Smith", "company_name": "Acme"})
    b = dedup_key({"full_name": "Jane Smith", "company_name": "Beta Inc"})
    assert a != b


def test_company_domain_key_normalizes():
    person = {"company_domain": "https://www.acme.com/about"}
    assert company_domain_key(person) == "acme.com"


def test_people_csv_fields_shape():
    for column in (
        "full_name", "title", "company_name", "email", "qualified", "qualification_reason", "source_provider",
    ):
        assert column in PEOPLE_CSV_FIELDS
