from leadorbyt.qualify_ml import profile_text


def test_profile_text_handles_person_shape():
    person = {"full_name": "Jane Smith", "title": "CISO", "company_name": "Acme Corp"}
    text = profile_text(person)
    assert text == "Jane Smith | CISO | Acme Corp"


def test_profile_text_person_and_business_fields_do_not_collide():
    business = {"business_name": "Acme Coffee", "category": "Coffee shop"}
    person = {"full_name": "Jane Smith", "title": "CISO"}
    assert profile_text(business) == "Acme Coffee | Coffee shop"
    assert profile_text(person) == "Jane Smith | CISO"
