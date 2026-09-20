from leadorbyt.web_signals import (
    author_from_url,
    build_query,
    emails_from_text,
    parse_ddg_html,
    source_from_url,
    unwrap_result_url,
)


class FakeSelector:
    def __init__(self, value=""):
        self._value = value

    def get(self, default=""):
        return self._value if self._value != "" else default


class FakeNode:
    def __init__(self, title, href, snippet):
        self._css_map = {
            "a.result__a": title,
            "a.result__a::attr(href)": href,
            ".result__snippet": snippet,
        }

    def css(self, selector):
        return FakeSelector(self._css_map.get(selector, ""))


class FakeSerp:
    def __init__(self, nodes):
        self._nodes = nodes
        self.status = 200
        self.body = b"x" * 400

    def css(self, selector):
        if selector == "div.result":
            return self._nodes
        return FakeSelector("")


def test_unwrap_ddg_redirect():
    wrapped = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.linkedin.com%2Fposts%2Fjane_looking-for-a-speaker"
    assert unwrap_result_url(wrapped) == "https://www.linkedin.com/posts/jane_looking-for-a-speaker"


def test_source_and_author_from_social_urls():
    assert source_from_url("https://www.linkedin.com/posts/ada-lovelace_speakers") == "linkedin"
    assert author_from_url("https://www.linkedin.com/posts/ada-lovelace_speakers", "linkedin") == "ada-lovelace"
    assert author_from_url("https://www.linkedin.com/in/ada-lovelace/", "linkedin") == "ada-lovelace"
    assert source_from_url("https://www.reddit.com/r/publicspeaking/comments/abc/need_a_keynote/") == "reddit"
    assert source_from_url("https://x.com/someorg/status/1") == "x"
    assert author_from_url("https://x.com/someorg/status/1", "x") == "someorg"
    assert source_from_url("https://www.facebook.com/groups/speakers/posts/9") == "facebook"


def test_emails_from_snippet():
    assert emails_from_text("email me at Book@Venue.com please") == "book@venue.com"
    assert emails_from_text("noreply@example.com") == ""


def test_build_query_includes_site_filter():
    query = build_query("speaking engagement", "Austin, TX", "site:linkedin.com")
    assert query == "speaking engagement Austin, TX site:linkedin.com"


def test_parse_ddg_html_extracts_social_posts():
    serp = FakeSerp(
        [
            FakeNode(
                "Looking for a keynote speaker in Austin",
                "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.linkedin.com%2Fposts%2Fjane_keynote",
                "Need a speaker for our Q4 offsite, DM me jane@corp.com",
            ),
            FakeNode(
                "Anyone know a local speaker?",
                "https://www.reddit.com/r/Austin/comments/xyz/speaker/",
                "Paying for a 45 min talk next month",
            ),
            FakeNode("DuckDuckGo help", "https://duckduckgo.com/help", "ignore"),
        ]
    )
    rows = parse_ddg_html(serp, "speaking engagement Austin site:linkedin.com")
    assert len(rows) == 2
    assert rows[0]["source"] == "linkedin"
    assert rows[0]["author"] == "jane"
    assert rows[0]["email"] == "jane@corp.com"
    assert rows[0]["url"] == "https://www.linkedin.com/posts/jane_keynote"
    assert rows[1]["source"] == "reddit"
    assert rows[1]["community"] == "r/Austin"
