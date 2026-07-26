from __future__ import annotations

import re


_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "bot_challenge",
        (
            r"sign in to confirm you.re not a bot",
            r"confirm you.re not a bot",
            r"unusual traffic from your computer network",
            r"automated queries",
            r"captcha",
        ),
    ),
    (
        "consent_challenge",
        (
            r"consent\.youtube\.com",
            r"before you continue to youtube",
            r"consent required",
        ),
    ),
    (
        "auth_required",
        (
            r"sign in to confirm your age",
            r"this video may be inappropriate for some users",
            r"members-only content",
            r"private video",
            r"login required",
            r"authentication required",
        ),
    ),
    (
        "service_unavailable",
        (
            r"video unavailable",
            r"this video is not available",
            r"not available in your country",
            r"geo.?restricted",
            r"premieres in",
        ),
    ),
    (
        "network_failure",
        (
            r"timed? ?out",
            r"connection (?:refused|reset|aborted)",
            r"network is unreachable",
            r"temporary failure in name resolution",
            r"name or service not known",
            r"proxy error",
            r"unable to connect",
        ),
    ),
    (
        "tooling_failure",
        (
            r"no supported javascript runtime",
            r"javascript runtime.*not (?:found|available)",
        ),
    ),
)


def classify_failure(message: str) -> str:
    """Map known service/tool errors to a stable verdict class.

    Unknown messages stay unknown instead of being promoted to a more precise
    failure layer without evidence.
    """

    normalized = " ".join(message.casefold().split())
    for outcome, patterns in _RULES:
        if any(re.search(pattern, normalized) for pattern in patterns):
            return outcome
    return "extractor_failure"
