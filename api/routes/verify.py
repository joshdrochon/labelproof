"""`POST /verify` — the whole product in one request (LP-074).

The route owns three things the pipeline deliberately does not: the upload boundary,
the clock, and the vocabulary of failure.

**The upload boundary.** Bytes arriving over HTTP are hostile until proven otherwise, so
they go through `pipeline.ingest` before anything else touches them — magic-byte sniffing
rather than a filename, caps enforced before decode, metadata stripped, re-encoded
(SEC-5, LP-076). That sequence — ingest, score, pre-gate — is `api.verify.prepare_images`
and is shared with batch rather than written twice, so the pre-gate cannot hold here and
silently not there.

**The clock.** `Config.request_budget_ms` is a deadline for the *request*, not a hope
about the provider. Extraction runs in a worker thread and is awaited with that deadline;
if it expires, the agent gets the partial picture and a Needs review, never a hang and
never a 504 (LP-079, PERF-7). "We ran out of time" and "we could not read it" are both
"this was not verified", which is the one thing a false pass can never be.

**The vocabulary.** Every failure leaves here as the error taxonomy, in a sentence a
compliance agent can act on (UX-6, OPS-5). Provider trouble is 503 — it is not our bug,
and the message says so without saying "inference".
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from pydantic import ValidationError

from api import errors
from api import logging as applog
from api.models import (
    Application,
    Cost,
    Recommendation,
    Timings,
    VerificationResult,
)
from api.provider.base import (
    ExtractionProvider,
    ImageInput,
    ProviderError,
    ProviderUsage,
)
from api.routes import get_config, provider_for
from api.verify import pregate_headline, prepare_images
from api.verify import unverified as _unverified
from api.verify import verify as run_verification

router = APIRouter()


#: Form-field names in the agents' words, not the schema's. A message that says
#: "brand_name: field required" is a message written for whoever wrote the schema.
_FIELD_LABELS: dict[str, str] = {
    "commodity": "commodity",
    "brand_name": "brand name",
    "class_type": "class or type designation",
    "alcohol_content": "alcohol content",
    "net_contents": "net contents",
    "producer_name": "producer name",
    "producer_address": "producer address",
    "country_of_origin": "country of origin",
    "is_import": "imported",
}


def parse_application(raw: str) -> Application:
    """Turn the `application` form part into an `Application`, or say why not (LP-075)."""
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise errors.UserError(
            "The application details could not be read. They must be sent as JSON in "
            "the 'application' part of the form. Re-enter the application and submit "
            "again — no images were checked.",
            next_step="fix_application",
            code="invalid_application_json",
        ) from exc

    if not isinstance(data, dict):
        raise errors.UserError(
            "The application details must be a set of fields, such as brand name and "
            "net contents. Re-enter the application and submit again.",
            next_step="fix_application",
            code="invalid_application_json",
        )

    # Sample files carry documentation keys; they are notes to a human, not fields.
    clean = {k: v for k, v in data.items() if not k.startswith("_")}

    try:
        return Application.model_validate(clean)
    except ValidationError as exc:
        raise errors.UserError(
            _validation_message(exc),
            next_step="fix_application",
            code="incomplete_application",
        ) from exc


def _validation_message(exc: ValidationError) -> str:
    problems: list[str] = []
    for error in exc.errors():
        location = str(error["loc"][0]) if error["loc"] else ""
        label = _FIELD_LABELS.get(location, location.replace("_", " ") or "application")
        kind = error["type"]
        if kind == "missing":
            problems.append(f"{label} is required")
        elif kind == "enum":
            problems.append(f"{label} must be one of spirits, wine or malt")
        elif kind.startswith("float") or kind.startswith("int"):
            problems.append(f"{label} must be a number, such as 45.0")
        elif kind.startswith("bool"):
            problems.append(f"{label} must be true or false")
        else:
            problems.append(f"{label} is not in a form this tool can read")

    detail = "; ".join(dict.fromkeys(problems)) or "some details are missing"
    return (
        f"The application details are incomplete: {detail}. Correct them and submit "
        f"again — no images were checked."
    )


async def _read_uploads(images: list[UploadFile], max_images: int) -> list[tuple[str, bytes]]:
    if len(images) > max_images:
        raise errors.UserError(
            f"That is more than {max_images} images. Upload the front and back of the "
            f"label.",
            next_step="reduce",
            code="too_many_images",
        )

    out: list[tuple[str, bytes]] = []
    for upload in images:
        out.append((upload.filename or "", await upload.read()))
    return out


@router.post("/verify", response_model=VerificationResult)
async def verify_endpoint(
    request: Request,
    images: Annotated[list[UploadFile], File()],
    application: Annotated[str, Form()],
    roles: Annotated[list[str] | None, Form()] = None,
) -> VerificationResult:
    """Verify one application against its label artwork."""
    started = time.perf_counter()
    config = get_config(request)
    request_id = applog.current_request_id() or applog.new_request_id()
    timings = Timings()

    parsed = parse_application(application)
    uploads = await _read_uploads(images, config.max_images)

    # Ingest, quality, and the pre-gate are `api.verify.prepare_images` — the same call
    # batch makes, so LP-321 cannot be true here and false there.
    prepared = prepare_images([data for _, data in uploads], config, roles=roles)
    timings.ingest = prepared.ingest_ms
    timings.quality = prepared.quality_ms
    reports = prepared.reports

    if prepared.pregated:
        timings.total = int((time.perf_counter() - started) * 1000)
        applog.log("verify_pregated", count=len(reports), duration_ms=timings.total)
        return _unverified(
            parsed,
            request_id=request_id,
            reports=reports,
            headline=pregate_headline(prepared.reason or ""),
            per_field="Not checked — the image could not be read.",
            timings=timings,
        )

    provider = provider_for(request, [name for name, _ in uploads])
    remaining_ms = config.request_budget_ms - (time.perf_counter() - started) * 1000

    result = await _verify_within_budget(
        parsed, prepared.usable, provider, remaining_ms=remaining_ms
    )

    if result is None:
        timings.total = int((time.perf_counter() - started) * 1000)
        applog.log(
            "verify_over_budget",
            duration_ms=timings.total,
            count=len(prepared.usable),
            recommendation=Recommendation.NEEDS_REVIEW.value,
        )
        seconds = config.request_budget_ms / 1000
        return _unverified(
            parsed,
            request_id=request_id,
            reports=reports,
            headline=(
                f"This check was stopped after {seconds:g} seconds, so the label was "
                f"not verified. Submit it again, or review it by hand. "
                f"The final decision is yours."
            ),
            per_field="Not checked — the check ran out of time before this row.",
            timings=timings,
        )

    result.request_id = request_id
    result.images = reports
    result.timings_ms.ingest = timings.ingest
    result.timings_ms.quality = timings.quality
    result.timings_ms.total = int((time.perf_counter() - started) * 1000)
    result.cost.usd = _estimated_usd(result.cost)

    applog.log(
        "verify_complete",
        recommendation=result.aggregate.recommendation.value,
        count=len(result.fields),
        duration_ms=result.timings_ms.total,
        input_tokens=result.cost.input_tokens,
        output_tokens=result.cost.output_tokens,
    )
    return result


def _estimated_usd(cost: Cost) -> float:
    """Price the token counts the pipeline already returned (OPS-4).

    The pricing table belongs to the provider layer, so this borrows it rather than
    copying the numbers — a second copy of a price list is a second thing to get wrong.
    Imported lazily and failure-tolerantly: a cost line is worth showing, and never worth
    failing a verification over.
    """
    if not (cost.input_tokens or cost.output_tokens):
        return 0.0
    try:
        from api.provider.anthropic_adapter import estimated_usd

        return estimated_usd(
            ProviderUsage(
                input_tokens=cost.input_tokens, output_tokens=cost.output_tokens
            )
        )
    except Exception:
        return 0.0


async def _verify_within_budget(
    application: Application,
    images: list[ImageInput],
    provider: ExtractionProvider,
    *,
    remaining_ms: float,
) -> VerificationResult | None:
    """Run the pipeline off the event loop, bounded by what is left of the budget.

    Returns None when the deadline wins. The worker thread is abandoned rather than
    killed — Python cannot interrupt it, and the honest options are "abandon it" or
    "wait for it", of which only one keeps the promise in PERF-7.
    """
    if remaining_ms <= 0:
        return None

    task = asyncio.create_task(
        asyncio.to_thread(run_verification, application, images, provider)
    )
    try:
        return await asyncio.wait_for(task, timeout=remaining_ms / 1000)
    except TimeoutError:
        return None
    except ProviderError as exc:
        applog.warn("provider_unavailable", kind="provider", code="provider_unavailable")
        raise errors.ProviderUnavailable() from exc
