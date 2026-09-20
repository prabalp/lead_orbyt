"""Export path for the Reddit signal-lead discovery pipeline (see signal_jobs.py).

Mirrors people_merge.py's role but for the signal schema (a Reddit post
expressing interest/pain, not a business or a verified contact) -- kept as
a separate module rather than overloading merge.py's/people_merge.py's fields.
"""

from pathlib import Path

from scrapling.spiders.result import ItemList

from .merge import export_csv

SIGNAL_CSV_FIELDS = [
    "reddit_username",
    "subreddit",
    "post_title",
    "post_body",
    "permalink",
    "created_utc",
    "matched_query",
    "qualified",
    "qualification_score",
    "qualification_reason",
    "discovered_by_query",
    "is_new_lead",
]


def dedup_key(signal: dict) -> str:
    """Stable identity for a discovered signal: the Reddit post id. A post
    has exactly one author, so no name-collision fallback is needed the way
    business/person dedup requires.
    """
    return f"reddit:{signal.get('reddit_post_id', '')}"


def export_signal_csv(rows: ItemList, path: str | Path) -> Path:
    return export_csv(rows, path, fields=SIGNAL_CSV_FIELDS)
