"""Adaptive job-title expansion for find_people_leads (see people_jobs.py).

A right-sized version of OpenOutFind's counting keyword-frontier
(reference/OpenOutFind's `vocabulary.py`/`select.py`: word tokens that
appear in accepted profiles grow the search vocabulary, sampled via a
Thompson/Beta-distributed frontier) -- without needing `openoutlearn` or a
`QueryNode` tree. Learns, per (user, ICP), which extra title-adjacent words
tend to show up in qualified vs rejected leads, and Thompson-samples from
that history to propose new title terms once a search's fixed job_titles
list runs dry.

Only active when a caller opts in via `goal_new_leads` (see
people_jobs.py) -- a plain search with no goal never touches this module.
"""

import random
import re

from . import store

_STOPWORDS = {
    "the", "and", "of", "for", "a", "an", "in", "at", "to", "on", "with",
    "&", "or", "is", "our", "us", "we",
}
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z\-]{2,}")


def extract_candidate_tokens(person: dict) -> set[str]:
    """Lowercased, stopword-filtered words from a person's job title --
    the field worth expanding a title-driven search on.
    """
    title = (person.get("title") or "").lower()
    return {word for word in _WORD_RE.findall(title) if word not in _STOPWORDS}


def record_outcome(user_id: str, icp_hash: str, person: dict, qualified: bool) -> None:
    """Bump accept/reject counts for every candidate token found in `person`."""
    for token in extract_candidate_tokens(person):
        store.bump_frontier_token(user_id, icp_hash, token, "title", accepted=qualified)


def sample_next_titles(user_id: str, icp_hash: str, existing_titles: list[str], n: int) -> list[str]:
    """Thompson-sample up to `n` new title terms not already in `existing_titles`.

    Each known frontier token gets a Beta(accept_count+1, reject_count+1)
    draw; the top-`n` draws are returned. A token seen only in rejected
    leads still has a chance of being sampled (Beta's still centered above
    0 with few reject-only observations), same as OpenOutFind's frontier
    never fully closing off a branch on one bad outcome.
    """
    existing_lower = {t.lower() for t in existing_titles}
    rows = store.get_frontier_tokens(user_id, icp_hash)
    candidates = [(token, accept, reject) for token, field, accept, reject in rows if token not in existing_lower]
    if not candidates:
        return []

    scored = [(token, random.betavariate(accept + 1, reject + 1)) for token, accept, reject in candidates]
    scored.sort(key=lambda pair: -pair[1])
    return [token for token, _ in scored[:n]]
