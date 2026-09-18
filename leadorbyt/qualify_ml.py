"""ML-based lead qualification against a caller-supplied Ideal Customer Profile.

Distinct from `qualify.py` (a free, flat rules-based pre-filter deciding
whether a discovered business is worth the paid `sources/registry.py`
fan-out): this module is an *opt-in, post-enrichment* step that scores each
merged lead against free-text ICP criteria supplied by the caller, only run
when `icp` is non-empty.

No API key, no LLM call, ever -- leadorbyt is an MCP server, and the agent
calling it (Claude or otherwise) already has its own reasoning available. So
rather than leadorbyt spending its own AI API key to judge a lead, this
module only ever does two things:

    1. A fully offline, local Gaussian Process ("the gate") learns to predict
       fit from whatever verdicts the calling agent has supplied so far for
       this (user, ICP) pair, via `submit_lead_verdicts` in server.py. No
       network call, no model download -- `HashingVectorizer` embeddings and
       `sklearn.gaussian_process.GaussianProcessRegressor`.
    2. Anything the gate isn't confident about (including every lead before
       any verdicts exist at all) comes back `source="agent_pending"`,
       fail-open (`qualified=True` -- a pending verdict never silently drops
       a lead). The calling agent previews these via `list_unlabeled_leads`,
       judges them itself, and reports back via `submit_lead_verdicts`,
       which is the *only* place a label is ever written.

This is a simplification of OpenOutFind's GP + LLM active-learning gate (see
reference/OpenOutFind's `core/ml/qualifier.py`) with the LLM call removed
entirely and replaced by the calling agent as the source of verdicts.
"""

import asyncio
import logging
import pickle
from dataclasses import dataclass
from typing import Literal

import numpy as np
from sklearn.feature_extraction.text import HashingVectorizer
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import RBF

from . import config, store

logger = logging.getLogger("leadorbyt.qualify_ml")

_EMBEDDING_DIM = 256
_vectorizer = HashingVectorizer(n_features=_EMBEDDING_DIM, alternate_sign=False, norm="l2")

QualificationSource = Literal["gate_accept", "gate_reject", "agent_pending"]


@dataclass
class QualificationResult:
    qualified: bool
    score: float
    reason: str
    source: QualificationSource


def profile_text(item: dict) -> str:
    """Build the text blob embedded for the gate (and shown to the agent via
    `list_unlabeled_leads`).

    Handles both discovery shapes leadorbyt produces -- a business (from
    discovery.py) and a person (from apollo_people.py) -- by pulling
    whichever fields are present; the two shapes don't overlap, so no
    branching on entity type is needed.
    """
    parts = [
        item.get("business_name", ""),
        item.get("category", ""),
        item.get("address", ""),
        item.get("website", ""),
        item.get("email", ""),
        item.get("storefront_platforms", ""),
        item.get("full_name", ""),
        item.get("title", ""),
        item.get("company_name", ""),
    ]
    return " | ".join(p for p in parts if p)


def embed(text: str) -> np.ndarray:
    """Deterministic, offline, fixed-shape (256,) embedding -- no model download."""
    return _vectorizer.transform([text]).toarray()[0]


def embedding_to_bytes(embedding: np.ndarray) -> bytes:
    """Canonical on-disk form for a qualification_labels.embedding_blob row."""
    return embedding.astype(np.float64).tobytes()


class ConfidenceGate:
    """Gaussian Process over lead embeddings, predicting P(qualified)."""

    def __init__(self):
        self._gp = GaussianProcessRegressor(kernel=RBF(length_scale=1.0), alpha=1e-2, normalize_y=False)
        self._fitted = False

    def fit(self, embeddings: np.ndarray, labels: np.ndarray) -> None:
        self._gp.fit(embeddings, labels)
        self._fitted = True

    @property
    def is_fitted(self) -> bool:
        return self._fitted

    def predict(self, embedding: np.ndarray) -> tuple[float, float]:
        """Returns (posterior mean, posterior std) for a single embedding."""
        if not self._fitted:
            raise RuntimeError("ConfidenceGate.predict called before fit()")
        mean, std = self._gp.predict(embedding.reshape(1, -1), return_std=True)
        return float(mean[0]), float(std[0])

    def to_bytes(self) -> bytes:
        return pickle.dumps(self._gp)

    @classmethod
    def from_bytes(cls, blob: bytes) -> "ConfidenceGate":
        gate = cls()
        gate._gp = pickle.loads(blob)
        gate._fitted = True
        return gate


def _fit_gate(labels: list[tuple[bytes, float]], icp: str) -> ConfidenceGate:
    """Fit a gate on real labels plus one synthetic positive anchor.

    The anchor -- the ICP text's own embedding, labeled positive -- gives the
    gate a concept of "what a match looks like" even from the very first
    agent-supplied verdict, instead of a degenerate fit on a handful of
    arbitrary points.
    """
    embeddings = [np.frombuffer(blob, dtype=np.float64) for blob, _ in labels]
    targets = [label for _, label in labels]
    embeddings.append(embed(icp))
    targets.append(1.0)
    gate = ConfidenceGate()
    gate.fit(np.array(embeddings), np.array(targets))
    return gate


def _gate_decision(mean: float, std: float) -> Literal["accept", "reject", "uncertain"]:
    if std > config.QUALIFY_GATE_MAX_STD:
        return "uncertain"
    if mean >= config.QUALIFY_GATE_CONFIDENCE:
        return "accept"
    if mean <= 1 - config.QUALIFY_GATE_CONFIDENCE:
        return "reject"
    return "uncertain"


def _domain_key(item: dict) -> str:
    """Informational identity for a qualification_labels row -- not used for
    lookup (labels are scoped by user_id+icp_hash only), just for debugging
    which lead a stored label came from. Works for both entity shapes.
    """
    return (
        item.get("website", "")
        or item.get("company_domain", "")
        or item.get("business_name", "")
        or item.get("full_name", "")
    )


async def qualify_lead(item: dict, icp: str, user_id: str) -> QualificationResult:
    """Qualify one lead. Thin wrapper over `qualify_pool` for a single item --
    kept for the business path (jobs.py) and as the simplest call shape for tests.
    """
    return (await qualify_pool([item], icp, user_id))[0]


async def pending_profiles(items: list[dict], icp: str, user_id: str) -> list[dict]:
    """Which of `items` the gate can't yet decide confidently -- the
    agent-qualify hand-off (list_unlabeled_leads in server.py). Read-only,
    makes no store writes. Each returned dict is the original item plus
    `profile_text`, in input order.
    """
    results = await qualify_pool(items, icp, user_id)
    pending = [
        (item, result) for item, result in zip(items, results) if result.source == "agent_pending"
    ]
    return [{**item, "profile_text": profile_text(item)} for item, _ in pending]


async def qualify_pool(items: list[dict], icp: str, user_id: str) -> list[QualificationResult]:
    """Qualify a whole batch of leads against `icp`, sharing one gate fit
    across the pool instead of refitting per lead.

    Every lead the gate can decide confidently (accept/reject, based on
    verdicts the calling agent has already supplied via `submit_lead_verdicts`)
    resolves immediately. Everything else -- including every lead at all
    before any verdicts exist for this (user, ICP) pair -- comes back
    `source="agent_pending"`, fail-open (`qualified=True`) so a pending
    verdict never silently drops a lead from the export. Use
    `list_unlabeled_leads`/`submit_lead_verdicts` to resolve those.
    """
    if not items:
        return []

    icp_hash = store.icp_hash(icp)
    embeddings = [embed(profile_text(item)) for item in items]

    labels = await asyncio.to_thread(store.get_qualification_labels, user_id, icp_hash)
    if len(labels) < config.QUALIFY_MIN_LABELS:
        return [
            QualificationResult(
                qualified=True,
                score=0.5,
                reason="Awaiting agent-supplied verdict -- see list_unlabeled_leads/submit_lead_verdicts",
                source="agent_pending",
            )
            for _ in items
        ]

    gate = _fit_gate(labels, icp)
    results: list[QualificationResult] = []
    for embedding in embeddings:
        mean, std = gate.predict(embedding)
        decision = _gate_decision(mean, std)
        if decision == "accept":
            results.append(
                QualificationResult(
                    qualified=True,
                    score=mean,
                    reason="High-confidence match to previously qualified leads",
                    source="gate_accept",
                )
            )
        elif decision == "reject":
            results.append(
                QualificationResult(
                    qualified=False,
                    score=mean,
                    reason="High-confidence match to previously rejected leads",
                    source="gate_reject",
                )
            )
        else:
            results.append(
                QualificationResult(
                    qualified=True,
                    score=0.5,
                    reason="Awaiting agent-supplied verdict -- see list_unlabeled_leads/submit_lead_verdicts",
                    source="agent_pending",
                )
            )
    return results
