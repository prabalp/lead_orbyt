from leadorbyt.errors import ErrorType, LeadOrbytError


def test_to_dict_shape():
    exc = LeadOrbytError(ErrorType.BLOCKED, "captcha detected", retryable=True)
    assert exc.to_dict() == {"type": "blocked", "message": "captcha detected", "retryable": True}


def test_to_dict_defaults_not_retryable():
    exc = LeadOrbytError(ErrorType.INVALID_INPUT, "bad niche")
    assert exc.to_dict()["retryable"] is False


def test_str_matches_stable_contract():
    exc = LeadOrbytError(ErrorType.TRANSPORT_ERROR, "timed out")
    assert str(exc) == "error: transport_error: timed out"
