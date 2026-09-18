from types import SimpleNamespace

from leadorbyt.discovery import _parse_place_page
from leadorbyt.merge import merge_records


class FakeSelector:
    def __init__(self, value=""):
        self._value = value

    def get(self, default=""):
        return self._value if self._value != "" else default

    def getall(self):
        if self._value == "":
            return []
        if isinstance(self._value, list):
            return self._value
        return [self._value]


class FakeResponse:
    def __init__(self, css_map, text="", url="https://www.google.com/maps/place/Acme/@30.1,-97.7,17z"):
        self._css_map = css_map
        self.url = url
        self._text = text

    def css(self, selector):
        return FakeSelector(self._css_map.get(selector, ""))

    def get_all_text(self, ignore_tags=()):
        return self._text

    def urljoin(self, href):
        return href


def test_parse_place_page_keeps_maps_contact_fields():
    response = FakeResponse(
        {
            "h1::text": "Acme Coffee",
            'a[data-item-id="authority"]::attr(href)': "https://acme.example",
            'button[data-item-id^="phone:tel:"]::attr(aria-label)': "Phone: (512) 555-0100",
            'button[data-item-id="address"]::attr(aria-label)': "Address: 1 Main St",
            'button[jsaction*="category"]::text': "Coffee shop",
            'button[data-item-id="oloc"]::attr(aria-label)': "Plus code: 8MC4+2X Austin",
            "a[href^='mailto:']::attr(href)": "mailto:hello@acme.example",
            "a[href^='tel:']::attr(href)": "tel:+15125550100",
            "a::attr(href)": [
                "https://www.instagram.com/acmecoffee",
                "https://www.facebook.com/acmecoffee",
            ],
        },
        text="Email hello@acme.example or call (512) 555-0100",
        url="https://www.google.com/maps/place/Acme/@30.2672,-97.7431,17z/data=!4m2",
    )

    item = _parse_place_page(response, response.url, "coffee shops | Austin, TX")

    assert item["business_name"] == "Acme Coffee"
    assert item["website"] == "https://acme.example"
    assert item["phone"] == "(512) 555-0100"
    assert item["email"] == "hello@acme.example"
    assert item["instagram"] == "https://www.instagram.com/acmecoffee"
    assert item["facebook"] == "https://www.facebook.com/acmecoffee"
    assert item["plus_code"] == "8MC4+2X Austin"
    assert item["lat"] == 30.2672
    assert item["lon"] == -97.7431
    assert item["maps_url"].startswith("https://www.google.com/maps/place/")


def test_merge_keeps_maps_values_and_fills_missing_from_scrape():
    discovery = {
        "business_name": "Acme Coffee",
        "category": "Coffee shop",
        "website": "https://acme.example",
        "phone": "(512) 555-0100",
        "email": "",
        "address": "1 Main St",
        "instagram": "https://www.instagram.com/acmecoffee",
        "facebook": "",
        "discovered_by_query": "coffee shops | Austin, TX",
    }
    enrichment = {
        "acme.example": {
            "email": "hello@acme.example",
            "phone": "999-0000",
            "instagram": "https://www.instagram.com/other",
            "facebook": "https://www.facebook.com/acmecoffee",
        }
    }

    row = merge_records([discovery], enrichment)[0]

    assert row["phone"] == "(512) 555-0100"
    assert row["email"] == "hello@acme.example"
    assert row["instagram"] == "https://www.instagram.com/acmecoffee"
    assert row["facebook"] == "https://www.facebook.com/acmecoffee"
