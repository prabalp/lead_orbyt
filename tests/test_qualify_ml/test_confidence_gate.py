import numpy as np
import pytest

from leadorbyt.qualify_ml import ConfidenceGate, _fit_gate, _gate_decision, embed


POSITIVE_TEXTS = ["Independent coffee shop with active Instagram", "Boutique cafe, loyal local following"]
NEGATIVE_TEXTS = ["Large chain fast food franchise", "Industrial parking lot operator"]


def _synthetic_gate() -> ConfidenceGate:
    embeddings = np.array([embed(t) for t in POSITIVE_TEXTS + NEGATIVE_TEXTS])
    labels = np.array([1.0, 1.0, 0.0, 0.0])
    gate = ConfidenceGate()
    gate.fit(embeddings, labels)
    return gate


def test_predict_before_fit_raises():
    gate = ConfidenceGate()
    with pytest.raises(RuntimeError):
        gate.predict(embed("anything"))


def test_gate_is_fitted_flag():
    gate = ConfidenceGate()
    assert gate.is_fitted is False
    gate.fit(np.array([embed("a"), embed("b")]), np.array([1.0, 0.0]))
    assert gate.is_fitted is True


def test_duplicate_of_positive_point_is_more_confident_than_novel_point():
    gate = _synthetic_gate()

    duplicate_mean, duplicate_std = gate.predict(embed(POSITIVE_TEXTS[0]))
    novel_mean, novel_std = gate.predict(embed("Something entirely unrelated to any training example"))

    assert duplicate_std < novel_std


def test_predictions_lean_toward_nearest_labeled_class():
    gate = _synthetic_gate()
    positive_mean, _ = gate.predict(embed(POSITIVE_TEXTS[0]))
    negative_mean, _ = gate.predict(embed(NEGATIVE_TEXTS[0]))
    assert positive_mean > negative_mean


def test_round_trip_serialization_preserves_predictions():
    gate = _synthetic_gate()
    blob = gate.to_bytes()
    restored = ConfidenceGate.from_bytes(blob)

    original_mean, _ = gate.predict(embed(POSITIVE_TEXTS[0]))
    restored_mean, _ = restored.predict(embed(POSITIVE_TEXTS[0]))
    assert original_mean == pytest.approx(restored_mean)


def test_fit_gate_adds_icp_anchor():
    labels = [(embed(NEGATIVE_TEXTS[0]).astype(np.float64).tobytes(), 0.0)]
    gate = _fit_gate(labels, icp="independent coffee shops")
    # With only one negative real label plus a positive ICP anchor, a lead
    # that looks like the ICP itself should score above one that doesn't.
    icp_like_mean, _ = gate.predict(embed("independent coffee shops"))
    unrelated_mean, _ = gate.predict(embed(NEGATIVE_TEXTS[0]))
    assert icp_like_mean > unrelated_mean


@pytest.mark.parametrize(
    "mean,std,expected",
    [
        (0.9, 0.05, "accept"),
        (0.05, 0.05, "reject"),
        (0.5, 0.05, "uncertain"),
        (0.95, 0.5, "uncertain"),
    ],
)
def test_gate_decision_thresholds(monkeypatch, mean, std, expected):
    from leadorbyt import config

    monkeypatch.setattr(config, "QUALIFY_GATE_CONFIDENCE", 0.85)
    monkeypatch.setattr(config, "QUALIFY_GATE_MAX_STD", 0.15)
    assert _gate_decision(mean, std) == expected
