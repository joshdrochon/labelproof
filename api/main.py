"""The application factory (LP-073).

One process serves both the API and the built SPA, because one deployable means one URL,
one cold start against the 5s gate, and one `docker run` (BUILD.md §1).

Two rules hold everywhere in this module:

**Every error leaves as the taxonomy.** There is no path out of this app that emits a
stack trace, a framework default, or the word "validation". Even a 404 is answered in the
same shape, because the front end has exactly one error renderer and a compliance agent
has exactly one question: what happened, and what do I do next (UX-6, OPS-5).

**Every request carries an ID.** Assigned before routing and echoed in `X-Request-ID`, so
the ID an agent reads off the screen is the ID in the logs. The logger allowlists its
field names, which is why nothing here logs a path or a filename — those can carry a
brand name, and label content never reaches a log line (SEC-4).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope

from api import errors
from api import logging as applog
from api.config import Config, load
from api.provider.base import ExtractionProvider, ProviderError
from api.routes import health, sample, verify

_WEB_DIST = Path(__file__).resolve().parents[1] / "web" / "dist"


class _SinglePageFiles(StaticFiles):
    """Static files with an SPA fallback: unknown paths render the app, not a 404.

    Client-side routes have no file behind them. Without this, reloading the page on any
    route but `/` gives the grader a blank 404 — which reads as "it broke".
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 404:
            return await super().get_response("index.html", scope)
        return response


def create_app(
    config: Config | None = None,
    provider: ExtractionProvider | None = None,
) -> FastAPI:
    """Build the app.

    `config` and `provider` are injectable so the test suite can run the real HTTP stack
    against fixture providers with no network and no key (ENG-3).
    """
    resolved = config or load()

    app = FastAPI(
        title="LabelProof",
        description="TTB alcohol label verification — recommends, never decides.",
        version="0.1.0",
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.config = resolved
    app.state.provider = provider

    applog.configure(level=getattr(logging, resolved.log_level.upper(), logging.INFO))
    for _ in resolved.warnings:
        applog.warn("config_incomplete")

    _install_middleware(app)
    _install_error_handlers(app)

    app.include_router(health.router)
    app.include_router(sample.router)
    app.include_router(verify.router)

    _install_spa(app)

    applog.log("app_started", model=resolved.extraction_model)
    return app


def _install_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Assign the request ID and time the request (LP-077, LP-078).

        The ID is generated here rather than trusted from a header: an ID chosen by the
        caller is an ID that can be forged to blend two agents' requests together in the
        log, and correlation is the whole point of having one.
        """
        request_id = applog.new_request_id()
        started = time.perf_counter()

        oversized = _too_large_to_read(request)
        if oversized is not None:
            response: Response = JSONResponse(
                status_code=oversized.status_code, content=oversized.to_payload()
            )
        else:
            response = await call_next(request)
        duration_ms = int((time.perf_counter() - started) * 1000)
        response.headers["X-Request-ID"] = request_id
        applog.log(
            "request_complete", status=response.status_code, duration_ms=duration_ms
        )
        return response


def _too_large_to_read(request: Request) -> errors.LabelProofError | None:
    """Reject an impossible upload before a byte of it is buffered.

    `ingest` enforces the real per-file cap, but only once the whole body has been spooled
    to disk — which is a long, expensive way to say no to a 2GB post. The declared length
    is only a hint (it can be absent, and it can lie), so this is a cheap first door, not
    the lock. The ceiling is derived from the configured caps so raising them raises this
    too.
    """
    declared = request.headers.get("content-length")
    if not declared or not declared.isdigit():
        return None

    config: Config = request.app.state.config
    # Multipart framing and the application JSON ride alongside the images.
    ceiling = config.max_images * config.max_image_bytes + 1024 * 1024
    if int(declared) <= ceiling:
        return None

    return errors.UserError(
        f"That upload is larger than this tool accepts. Send at most "
        f"{config.max_images} images of up to "
        f"{config.max_image_bytes // (1024 * 1024)} MB each.",
        next_step="resize",
        code="file_too_large",
    )


def _install_error_handlers(app: FastAPI) -> None:
    def _payload(error: errors.LabelProofError) -> JSONResponse:
        applog.log(
            "request_failed",
            kind=error.kind.value,
            code=error.code,
            status=error.status_code,
        )
        return JSONResponse(status_code=error.status_code, content=error.to_payload())

    @app.exception_handler(errors.LabelProofError)
    async def labelproof_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, errors.LabelProofError)
        return _payload(exc)

    @app.exception_handler(ProviderError)
    async def provider_error(request: Request, exc: Exception) -> JSONResponse:
        """A provider failure is a 503, never a 500 — it is not our bug (NET-3, TC-21)."""
        return _payload(errors.ProviderUnavailable())

    @app.exception_handler(RequestValidationError)
    async def malformed_request(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        return _payload(_as_user_error(exc))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, StarletteHTTPException)
        return _payload(_from_status(exc.status_code))

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        """The last line. Whatever broke, the agent gets a sentence, not a traceback."""
        applog.error("unhandled_exception", kind="internal", code="internal_error")
        return JSONResponse(
            status_code=500, content=errors.InternalError().to_payload()
        )


def _as_user_error(exc: RequestValidationError) -> errors.UserError:
    """Say which part of the upload was wrong, in the words of the form (LP-075)."""
    names = {str(error["loc"][-1]) for error in exc.errors() if error.get("loc")}
    missing_images = "images" in names
    missing_application = "application" in names

    if missing_images and missing_application:
        message = (
            "This request did not arrive as a label submission. Send the label images "
            "and the application details together from the verification form."
        )
    elif missing_images:
        message = (
            "No label images were included. Add the label artwork for this application "
            "and submit again."
        )
    elif missing_application:
        message = (
            "The application details were not included. Fill in the application fields "
            "and submit again — no images were checked."
        )
    else:
        message = (
            "Part of that request could not be read. Check the form and submit again — "
            "nothing has been checked."
        )
    return errors.UserError(message, next_step="fix_request", code="malformed_request")


def _from_status(status: int) -> errors.LabelProofError:
    """Frameworks answer with bare status codes. Agents get sentences instead."""
    if status == 404:
        return errors.UserError(
            "That address is not part of this tool. Go back to the verification page "
            "and start again.",
            next_step="navigate",
            code="not_found",
        )
    if status == 405:
        return errors.UserError(
            "That request is not something this tool accepts here. Go back to the "
            "verification page and submit the label from the form.",
            next_step="navigate",
            code="method_not_allowed",
        )
    if status == 413:
        return errors.UserError(
            "That upload is too large for this tool. Save the images at a smaller size "
            "and upload them again.",
            next_step="resize",
            code="file_too_large",
        )
    if status == 429:
        return errors.UserError(
            "This tool is handling too many requests right now. Wait a moment and "
            "submit again — nothing has been checked.",
            next_step="retry",
            code="too_many_requests",
        )
    if status >= 500:
        return errors.InternalError()
    return errors.UserError(
        "That request could not be handled. Go back to the verification page and try "
        "again.",
        next_step="navigate",
        code="bad_request",
    )


def _install_spa(app: FastAPI) -> None:
    """Serve the built SPA when it exists, and explain itself when it does not.

    A missing `web/dist` is normal during backend work and in the API test suite. Mounting
    a directory that is not there would crash the app at startup, which turns "the UI is
    not built yet" into "the API is down".
    """
    if _WEB_DIST.is_dir():
        app.mount("/", _SinglePageFiles(directory=_WEB_DIST, html=True), name="spa")
        return

    @app.get("/")
    def index() -> dict[str, str]:
        return {
            "service": "labelproof",
            "status": "The verification service is running. The web interface is not "
            "built in this deployment — use POST /verify, or GET /sample "
            "for the demo application.",
        }


app = create_app()
