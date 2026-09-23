import pytest

from leadorbyt import org_search, web_search


def test_domain_of_strips_www():
    assert org_search._domain_of("https://www.acme.org/about") == "acme.org"
    assert org_search._domain_of("https://acme.org") == "acme.org"


def test_is_excluded_matches_domain_and_subdomains():
    assert org_search._is_excluded("wikipedia.org")
    assert org_search._is_excluded("en.wikipedia.org")
    assert org_search._is_excluded("linkedin.com")
    assert not org_search._is_excluded("nationalsalesassociation.org")


def test_dedup_key_prefers_domain():
    assert org_search.dedup_key({"domain": "acme.org", "org_name": "Acme"}) == "org:acme.org"
    assert org_search.dedup_key({"domain": "", "org_name": "Acme"}) == "org:acme"


def test_query_variants_include_directory_style_terms():
    variants = org_search._query_variants("sales leadership association", "Texas")
    assert "sales leadership association Texas" in variants
    assert any("list of" in v for v in variants)
    assert any("directory" in v for v in variants)


@pytest.mark.asyncio
async def test_discover_organizations_excludes_aggregator_domains(monkeypatch):
    async def fake_search_web(query, max_results=10):
        return [
            {"title": "Sales Leaders Assoc", "url": "https://salesleaders.org", "snippet": "A trade group"},
            {"title": "Wikipedia list", "url": "https://en.wikipedia.org/wiki/List", "snippet": "..."},
            {"title": "Sales Leaders on LinkedIn", "url": "https://linkedin.com/company/sla", "snippet": "..."},
        ]

    monkeypatch.setattr(web_search, "search_web", fake_search_web)

    orgs = await org_search.discover_organizations("sales leadership association")

    assert len(orgs) == 1
    assert orgs[0]["domain"] == "salesleaders.org"
    assert orgs[0]["org_name"] == "Sales Leaders Assoc"


@pytest.mark.asyncio
async def test_discover_organizations_dedups_across_query_variants(monkeypatch):
    calls = {"n": 0}

    async def fake_search_web(query, max_results=10):
        calls["n"] += 1
        return [{"title": "Same Org", "url": "https://sameorg.org", "snippet": "s"}]

    monkeypatch.setattr(web_search, "search_web", fake_search_web)

    orgs = await org_search.discover_organizations("some category", max_results=20)

    assert calls["n"] > 1  # multiple query variants really were tried
    assert len(orgs) == 1  # but the same domain across variants isn't duplicated


@pytest.mark.asyncio
async def test_discover_organizations_stops_at_max_results(monkeypatch):
    async def fake_search_web(query, max_results=10):
        return [
            {"title": f"Org {i} for {query}", "url": f"https://org{i}-{hash(query)}.org", "snippet": "s"}
            for i in range(10)
        ]

    monkeypatch.setattr(web_search, "search_web", fake_search_web)

    orgs = await org_search.discover_organizations("some category", max_results=3)

    assert len(orgs) == 3
