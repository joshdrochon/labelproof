"""Provider abstraction — the single choke point every AI call passes through.

Two reasons this interface exists, and both are requirements rather than taste:

**NET-4.** The customer runs Azure behind a FedRAMP boundary. Swapping to a gov-cloud
endpoint must be an adapter change, not a refactor.

**ENG-3.** CI must pass with no network. Every test that would call a model goes through
`FakeProvider` instead, so the suite is deterministic and offline. The deployed app runs
live; the test suite must never need to.

The browser never talks to a provider (NET-2) — everything here is server-side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from api.models import Commodity, Extraction


@dataclass(frozen=True)
class ImageInput:
    """One preprocessed image, ready for extraction."""

    index: int
    data: bytes
    media_type: str = "image/png"
    role: str | None = None


@dataclass(frozen=True)
class ExtractionRequest:
    commodity: Commodity
    images: list[ImageInput]


@dataclass
class ProviderUsage:
    """Token and cost accounting, captured on every call from day one (OPS-4)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    #: Tokens written INTO the cache, billed at 1.25x input. Counted separately because
    #: they are neither free nor the same price as anything else, and because omitting
    #: them under-reports every cold request — which is every first click a grader makes.
    cache_creation_tokens: int = 0

    model: str = ""

    def merge(self, other: ProviderUsage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.model = self.model or other.model


@dataclass
class ExtractionResponse:
    extractions: list[Extraction] = field(default_factory=list)
    usage: ProviderUsage = field(default_factory=ProviderUsage)
    latency_ms: int = 0


class ProviderError(Exception):
    """The provider could not be reached or returned something unusable.

    Callers translate this into a plain-language degradation message. It must never
    surface as a stack trace or a hang (NET-3, TC-21).
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@runtime_checkable
class ExtractionProvider(Protocol):
    """Every AI extraction call in the system goes through this."""

    name: str

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        """Read the labels. Raises ProviderError when the provider is unusable.

        Implementations must never invent a field value. A field that cannot be read is
        reported with `value=None` and `legible=False` — there is no channel for a guess,
        and `ExtractedField` has no field to put one in (LP-067).
        """
        ...
