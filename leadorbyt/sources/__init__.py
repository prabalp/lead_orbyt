"""Optional third-party data-source clients, each gated by its own API key in `config`.

Use `sources.registry.enrich_extras(...)` to query every configured source
for one business at once; see registry.py for the full list and
`.env.example` at the repo root for every key it reads.
"""

from .registry import enrich_extras

__all__ = ["enrich_extras"]
