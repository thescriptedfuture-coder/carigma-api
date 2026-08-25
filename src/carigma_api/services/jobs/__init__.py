"""The jobs layer.

Source-agnostic by construction: `models` defines the shape, `adapter` defines
the interface and the machinery, `providers` holds the only code that knows any
specific API. Adding a provider means adding to `providers` and registering it —
nothing else changes.
"""

from carigma_api.services.jobs.adapter import (
    JobProvider,
    JobsGateway,
    ProviderUnavailable,
    QuotaExhausted,
    dedupe_across_sources,
)
from carigma_api.services.jobs.models import ApplyOption, NormalizedJob
from carigma_api.services.jobs.providers import AdzunaProvider, JSearchProvider

__all__ = [
    "AdzunaProvider",
    "ApplyOption",
    "JSearchProvider",
    "JobProvider",
    "JobsGateway",
    "NormalizedJob",
    "ProviderUnavailable",
    "QuotaExhausted",
    "dedupe_across_sources",
]
