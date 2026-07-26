import pytest

from cfwarp_service_eval.classify import classify_failure


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. This helps protect our community.",
            "bot_challenge",
        ),
        ("YouTube said CAPTCHA required due to unusual traffic", "bot_challenge"),
        ("Redirected to consent.youtube.com before extraction", "consent_challenge"),
        ("ERROR: This video is private. Login required", "auth_required"),
        ("ERROR: Video unavailable in your country", "service_unavailable"),
        ("Unable to connect to proxy: connection refused", "network_failure"),
        ("No supported JavaScript runtime could be found", "tooling_failure"),
        ("Extractor changed in an unrecognized way", "extractor_failure"),
    ],
)
def test_classify_failure(message: str, expected: str) -> None:
    assert classify_failure(message) == expected
