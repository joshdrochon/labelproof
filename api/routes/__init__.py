"""Route package — shared request-scoped wiring, and nothing else.

Only two things live here, and both are wiring rather than behaviour: how a request
finds its `Config`, and how it finds an `ExtractionProvider`. Everything else belongs in
a route module or, better, in the rules engine.

The router assembly deliberately lives in `api/main.py` instead of this file. If this
module imported the route modules, and the route modules imported this module for
`provider_for`, the package would import itself mid-initialisation — the kind of cycle
that works until an import order changes and then fails at startup on the grader's
machine.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import Request

from api import errors
from api.config import Config
from api.provider.base import ExtractionProvider


def get_config(request: Request) -> Config:
    """The config validated once at startup (LP-087). Never re-read per request."""
    config: Config = request.app.state.config
    return config


def provider_for(request: Request, filenames: Sequence[str] = ()) -> ExtractionProvider:
    """Resolve the extraction provider for this request.

    Resolution order, most explicit first:

    1. A provider injected into `create_app(provider=...)` or set on `app.state`. This is
       how the test suite runs entirely offline (ENG-3) — it hands the app a
       `SpecBackedProvider` or a `FailingProvider` and no socket is ever opened.
    2. Fake mode (`LABELPROOF_FAKE_PROVIDER=1`), which replays the generated fixtures.
       `filenames` picks the fixture, so the demo and the E2E suite exercise real
       fixture data without a key.
    3. The live adapter.

    The live adapter is imported lazily and its absence is reported as a provider
    outage rather than an ImportError, because that is what it is from the agent's
    seat: the label reading service is not available, and no application data changed.
    """
    injected = getattr(request.app.state, "provider", None)
    if injected is not None:
        provider: ExtractionProvider = injected
        return provider

    config = get_config(request)
    if config.use_fake_provider:
        return _fixture_provider(filenames)
    return _live_provider(request, config)


def _fixture_provider(filenames: Sequence[str]) -> ExtractionProvider:
    """Fixture replay keyed off the uploaded filename.

    FAILS CLOSED on an unrecognized name. An earlier version fell back to the clean Old
    Tom fixture, which meant uploading arbitrary bytes under any filename returned
    `ready_to_approve` with a verbatim government warning that was never on the image —
    a false pass with fabricated evidence, and the exact failure the PRD names as the
    worst this product can produce.

    Sample mode replays *recorded* labels. It cannot read pixels, so when it does not
    recognize a filename the only honest answer is that nothing was checked.
    """
    from api.provider.fake import SpecBackedProvider, spec_name_for_image
    from fixtures.generator.catalog import by_name

    for name in filenames:
        key = spec_name_for_image(name)
        if key is None:
            continue
        try:
            return SpecBackedProvider(by_name(key))
        except KeyError:
            continue

    raise errors.ProviderUnavailable(
        "This server is running in sample mode, which can only check the built-in "
        "example labels — it cannot read an uploaded photo. Nothing has been checked. "
        "Use the \u201cTry a sample\u201d button, or ask whoever runs this service to "
        "add the label reading service.",
        next_step="try_sample",
    )


def _live_provider(request: Request, config: Config) -> ExtractionProvider:
    """Build the live adapter once and keep it on the app.

    Per-request construction would throw away the connection pool and reset the circuit
    breaker on every call — a breaker that forgets has learned nothing, and the breaker
    is what keeps a provider outage from becoming a queue of hanging requests.
    """
    if not config.anthropic_api_key:
        raise errors.ProviderUnavailable(
            "The label reading service is not set up on this server yet, so nothing "
            "can be checked. Ask whoever runs this service to add the API key, or "
            "switch the service to sample mode."
        )

    try:
        from api.provider.anthropic_adapter import AnthropicVisionProvider

        live: ExtractionProvider = AnthropicVisionProvider(config)
    except Exception as exc:
        raise errors.ProviderUnavailable(
            "The label reading service is not available on this server, so nothing "
            "has been checked. Try again shortly, or ask whoever runs this service "
            "to check it."
        ) from exc

    request.app.state.provider = live
    return live
