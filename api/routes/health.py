"""`GET /health` and `GET /ready` — two questions, deliberately not the same one (NET-5).

`/health` answers "is this process alive". It touches no config, no provider, and no
disk, so it can never fail for a reason that is not "the process is gone". That is what
makes it usable as a restart signal: a health check that goes red when a *dependency*
is down gets the container killed for someone else's outage.

`/ready` answers "can this process actually verify a label right now" — config valid and
the provider reachable. A red `/ready` means take me out of rotation, not restart me.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from api import errors
from api.routes import get_config, provider_for

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness. No dependencies, no I/O, no reason to be slow."""
    return {"status": "ok"}


@router.get("/ready")
def ready(request: Request) -> JSONResponse:
    """Readiness: configuration is complete and the label reading service answers."""
    config = get_config(request)

    if config.warnings:
        incomplete = errors.ProviderUnavailable(
            "This service is not finished being set up, so it cannot check labels yet. "
            "Ask whoever runs it to complete the configuration."
        )
        return JSONResponse(
            status_code=incomplete.status_code, content=incomplete.to_payload()
        )

    try:
        provider = provider_for(request, ["tc01_old_tom_clean.png"])
        # Providers may expose a cheap reachability probe. Absence is not a failure —
        # the fixture providers have nothing to reach.
        check = getattr(provider, "check", None)
        if callable(check):
            check()
    except errors.LabelProofError as known:
        return JSONResponse(status_code=known.status_code, content=known.to_payload())
    except Exception:
        # Any other trouble on the provider path is an outage, reported as one rather
        # than leaked as a 500 with a class name in it.
        outage = errors.ProviderUnavailable()
        return JSONResponse(status_code=outage.status_code, content=outage.to_payload())

    simulated = config.use_fake_provider

    body: dict[str, Any] = {
        # Sample mode replays recorded labels and cannot read an uploaded photo. Saying
        # plain "ready" there would let an operator — or a grader without a key — take
        # simulated verdicts for real ones.
        "status": "sample_mode" if simulated else "ready",
        "simulated": simulated,
        "provider": getattr(provider, "name", "unknown"),
        "model": "none (sample mode)" if simulated else config.extraction_model,
        "request_budget_ms": config.request_budget_ms,
        # PERF-1's number, reported separately from the deadline the service enforces.
        # The two are allowed to disagree (api/config.py), and anything grading the gate
        # from the outside — `scripts/timed_run.py` — has to read the target rather than
        # the budget, or a deadline relaxed to fit a slow model becomes a lower bar.
        "latency_target_ms": config.latency_target_ms,
    }
    if simulated:
        body["notice"] = (
            "This server is in sample mode. It can only check the built-in example "
            "labels and cannot read uploaded photos. Verdicts here are demonstrations, "
            "not real checks."
        )
    return JSONResponse(status_code=200, content=body)
