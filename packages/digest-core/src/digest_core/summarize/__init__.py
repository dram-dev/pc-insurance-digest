"""digest_core.summarize — shared summarizer transport.

Backends are pure transport (no domain prompt/content): a domain passes its
system prompt + a BackendConfig built from its settings.
"""
from digest_core.summarize.backends import BACKENDS, BackendConfig, BackendError

__all__ = ["BACKENDS", "BackendConfig", "BackendError"]
