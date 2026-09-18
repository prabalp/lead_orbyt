import numpy as np

from leadorbyt.qualify_ml import _EMBEDDING_DIM, embed, profile_text


def test_embed_is_deterministic():
    a = embed("Acme Coffee | Coffee shop")
    b = embed("Acme Coffee | Coffee shop")
    assert np.array_equal(a, b)


def test_embed_has_fixed_shape():
    assert embed("short").shape == (_EMBEDDING_DIM,)
    assert embed("a much longer piece of text " * 20).shape == (_EMBEDDING_DIM,)


def test_embed_differs_for_different_text():
    a = embed("Acme Coffee")
    b = embed("Totally Different Business")
    assert not np.array_equal(a, b)


def test_profile_text_skips_blank_fields():
    text = profile_text({"business_name": "Acme", "category": "", "website": "https://acme.com"})
    assert text == "Acme | https://acme.com"


def test_profile_text_empty_item():
    assert profile_text({}) == ""
