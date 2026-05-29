"""digest_core.summarize — shared summarizer transport.

Backends are pure transport (no domain prompt/content): a domain passes its
system prompt + a BackendConfig built from its settings.
"""
from digest_core.summarize.backends import BACKENDS, BackendConfig, BackendError
from digest_core.summarize.runner import enforce_topic_caps, extract_json

__all__ = [
    "BACKENDS", "BackendConfig", "BackendError",
    "enforce_topic_caps", "extract_json",
]
