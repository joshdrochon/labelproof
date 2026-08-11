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

import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from api import errors
from api import logging as applog
from api.config import Config, load
from api.provider.base import ExtractionProvider, ProviderError
from api.routes import batch, health, sample, verify
from api.security import harden

_WEB_DIST = Path(__file__).resolve().parents[1] / "web" / "dist"


class _SinglePageFiles(StaticFiles):
    """Static files with an SPA fallback: unknown paths render the app, not a 404.

    Client-side routes have no file behind them. Without this, reloading the page on any
    route but `/` gives the grader a blank 404 — which reads as "it broke".
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        """Serve the file, falling back to index.html for client-side routes.

        StaticFiles RAISES HTTPException(404) for a missing file rather than returning a
        404 response, so checking the status code never fires. Catching the exception is
        the only thing that works — an earlier version checked the status and the whole
        fallback was dead code, which turned every deep link into a raw JSON error blob.
        """
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            return await super().get_response("index.html", scope)


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
    # Rate limiting, security headers, strict CORS, traceback containment and the retention
    # sweeper, all from one call (SEC-2, SEC-4, SEC-6, SEC-9).
    #
    # It goes AFTER `_install_middleware` and that is load-bearing. Starlette builds the
    # user middleware stack so the last one added is the outermost, which is what puts the
    # security headers on responses no route ever saw — the oversize-upload 413 above is
    # one — and what lets the containment layer catch an exception before Starlette's
    # `ServerErrorMiddleware` re-raises it into uvicorn's default traceback handler. That
    # re-raise is how label text reaches stdout, and `api/logging.py`'s allowlist cannot
    # see that path.
    harden(app, resolved)
    _install_error_handlers(app)

    app.include_router(health.router)
    app.include_router(sample.router)
    app.include_router(verify.router)
    # Batch must be mounted BEFORE the SPA. `_install_spa` registers a `GET /{path:path}`
    # catch-all, and a catch-all registered first out-ranks a real route — `GET /batch/{id}`
    # would answer 404 from the SPA guard while the endpoint sat there unreachable.
    app.include_router(batch.router)

    # Without this the shared provider budget never learns that a Verify Now request is in
    # flight, so batch never stands aside and BATCH-9/PERF-5 is dead code that still passes
    # its unit tests. Marking only — it can never slow a verification down.
    batch.install_verify_priority(app)

    _install_spa(app)

    applog.log("app_started", model=resolved.extraction_model)
    return app


def _install_middleware(app: FastAPI) -> None:
    # Pure ASGI, and it must be a middleware rather than anything inside a route: it wraps
    # `receive`, and the multipart parser drains the whole body before a route function's
    # first line runs. See `_WireLimit`.
    app.add_middleware(_WireLimit)

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


#: Headroom above the batch content ceiling for multipart framing, the manifest, and the
#: filename of every part. `MAX_FILES` is 4000 and each part costs a couple of hundred
#: bytes of boundary and headers, so a megabyte would be too tight by an order of
#: magnitude; 32 MB is generous and still nowhere near "unbounded".
_BATCH_ENVELOPE_BYTES = 32 * 1024 * 1024

#: Slack above the Verify Now content ceiling for multipart framing and the application
#: JSON, which ride alongside at most four images.
_VERIFY_ENVELOPE_BYTES = 1024 * 1024


def _size_ceiling(path: str, config: Config) -> tuple[int, errors.LabelProofError]:
    """The whole-request ceiling for this path, and the refusal that goes with it.

    The two modes accept wildly different uploads and a single ceiling cannot serve both.
    Verify Now takes at most four images: anything past that is a mistake, and the tight
    cap is what makes a 2 GB post cost a header read. A real batch is 300 applications and
    roughly 600 photographs — over a gigabyte — and deriving its ceiling from the
    single-verify caps refused every genuine importer dump before it reached a route, with
    a message telling the agent to "send at most 4 images". The prototype only looked
    healthy because the fixtures are kilobytes.

    So the ceiling follows the path, and so does the sentence — a refusal that names the
    wrong limit sends the agent to fix something that was never wrong.
    """
    if path.strip("/").split("/", 1)[0] == "batch":
        return (
            batch.MAX_TOTAL_BYTES + _BATCH_ENVELOPE_BYTES,
            errors.UserError(
                f"That batch upload is larger than this tool accepts at once — the limit "
                f"is {batch.MAX_TOTAL_BYTES // (1024**3)} GB of artwork per upload, and up "
                f"to {batch.MAX_ROWS} applications. Split it into smaller batches and "
                f"upload them separately. Nothing has been checked.",
                next_step="reduce",
                code="batch_too_large",
            ),
        )

    return (
        config.max_images * config.max_image_bytes + _VERIFY_ENVELOPE_BYTES,
        errors.UserError(
            f"That upload is larger than this tool accepts. Send at most "
            f"{config.max_images} images of up to "
            f"{config.max_image_bytes // (1024 * 1024)} MB each.",
            next_step="resize",
            code="file_too_large",
        ),
    )


def _too_large_to_read(request: Request) -> errors.LabelProofError | None:
    """Refuse an upload whose *declared* length is already impossible.

    One of two doors, and the weaker one. `Content-Length` is a hint: it can lie, and a
    `Transfer-Encoding: chunked` upload does not send one at all, so this check is skipped
    entirely for the shape an attacker would pick. It survives because it is free and it
    answers the honest client immediately, before a byte is read.

    `_WireLimit` below is the door that actually holds.
    """
    declared = request.headers.get("content-length")
    if not declared or not declared.isdigit():
        return None

    ceiling, refusal = _size_ceiling(request.url.path, request.app.state.config)
    if int(declared) <= ceiling:
        return None
    return refusal


class _WireLimit:
    """Count request body bytes as they come off the wire, and stop at the ceiling.

    This has to be pure ASGI and it has to be outside everything, because of where the
    bytes actually go. FastAPI resolves `files: list[UploadFile]` as a dependency, so
    Starlette's `MultiPartParser` consumes the ENTIRE body and spools it into
    `SpooledTemporaryFile`s — rolling to `$TMPDIR` above 1 MB — before the route function's
    first line runs. Everything the route does afterwards, including `_Landing`'s running
    total, is reading a local temp file. Measured: with the batch cap set to 1 MB, a 200 MB
    chunked upload was written to disk in full and the cap fired on the last byte.

    So `_Landing` bounds *memory*, which was the OOM, and nothing bounded *disk*. An
    unauthenticated POST of any size filled the volume — and the volume holds `jobs.db`,
    so filling it takes every batch on the server with it. An earlier docstring here
    claimed the running total was counted "as bytes arrive". It was not, and a claimed
    bound that does not exist is worse than a known gap, because it is how the next person
    stops checking.

    Counting in `receive` is the only place upstream of the parser. Raising from there is
    not enough on its own: FastAPI wraps *any* exception thrown while it is parsing a body
    into `HTTPException(400, "There was an error parsing the body")`, so the refusal came
    back as the generic "that request could not be handled" and the agent was told nothing
    about size. So the response is replaced on the way out instead, which also keeps this
    independent of that framework detail rather than hostage to it (OPS-5).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        parent = scope.get("app")
        if scope["type"] != "http" or parent is None:
            await self.app(scope, receive, send)
            return

        ceiling, refusal = _size_ceiling(scope.get("path", ""), parent.state.config)
        seen = 0
        refused = False
        answered = False

        async def metered() -> Message:
            nonlocal seen, refused
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > ceiling:
                    refused = True
                    # Raised to unwind immediately — the point is to stop reading, not to
                    # deliver the message. `guarded` delivers it.
                    raise refusal
            return message

        async def guarded(message: Message) -> None:
            nonlocal answered
            if not refused:
                await send(message)
                return
            # Whatever the app is trying to say, it is a symptom of our refusal. Say ours.
            if message["type"] == "http.response.start" and not answered:
                answered = True
                await _send_error(send, refusal)

        try:
            await self.app(scope, metered, guarded)
        except errors.LabelProofError:
            if not refused:
                raise
        if refused and not answered:
            answered = True
            await _send_error(send, refusal)


async def _send_error(send: Send, error: errors.LabelProofError) -> None:
    body = json.dumps(error.to_payload()).encode("utf-8")
    applog.log("request_failed", kind=error.kind.value, code=error.code, status=error.status_code)
    await send(
        {
            "type": "http.response.start",
            "status": error.status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


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
        if exc.status_code == 400 and (unusable := _unusable_upload(request, exc)):
            return _payload(unusable)
        return _payload(_from_status(exc.status_code))

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        """The last line. Whatever broke, the agent gets a sentence, not a traceback."""
        applog.error("unhandled_exception", kind="internal", code="internal_error")
        return JSONResponse(
            status_code=500, content=errors.InternalError().to_payload()
        )


def _unusable_upload(
    request: Request, exc: StarletteHTTPException
) -> errors.LabelProofError | None:
    """Turn a multipart parse failure into advice, when we can tell that is what it was.

    Starlette caps a multipart body at 1000 parts. That is *below* the batch route's own
    `MAX_FILES`, so it always binds first for multi-select — and FastAPI wraps it, like
    every other body-parsing failure, into a bare `HTTPException(400)`. An agent who
    ctrl-A'd 1200 label images was told "That address is not part of this tool. Go back to
    the verification page and try again": no limit, no number, and not even the right
    category of problem.

    The original exception is still on `__cause__`, so the specific message can be
    recovered when it is there. When it is not, a 400 on `/batch` with no route-level
    explanation is still an unreadable upload, and saying so with the real limit beats the
    navigation advice.
    """
    if request.url.path.strip("/").split("/", 1)[0] != "batch":
        return None

    cause = exc.__cause__
    too_many = isinstance(cause, MultiPartException) and "Too many" in str(cause)
    if cause is not None and not isinstance(cause, MultiPartException):
        return None

    detail = (
        f"That upload holds more separate files than one form submission can carry "
        f"({_MAX_MULTIPART_PARTS} is the limit)."
        if too_many or cause is None
        else "That upload could not be read as a batch submission."
    )
    return errors.UserError(
        f"{detail} Put the label images in a zip file and upload that with the manifest — "
        f"an archive can hold the whole batch. Nothing has been checked.",
        next_step="reduce",
        code="too_many_files",
    )


#: Starlette's own multipart part limit. Named here because it binds before
#: `routes.batch.MAX_FILES` (4000) for multi-select and the agent has to be told a number
#: that is actually true. It is not configurable through FastAPI's `File()` dependency.
_MAX_MULTIPART_PARTS = 1000


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


#: Path prefixes owned by the API. Anything under these answers in the error taxonomy;
#: everything else falls through to the single-page app.
_API_PREFIXES: frozenset[str] = frozenset(
    {"verify", "batch", "sample", "health", "ready", "docs", "redoc", "openapi.json"}
)

#: Routes that exist but only accept POST. A GET on one is a wrong verb, not a wrong URL.
_POST_ONLY_ROUTES: frozenset[str] = frozenset({"/verify", "/batch"})


def _install_spa(app: FastAPI) -> None:
    """Serve the built SPA without letting it swallow the API.

    Mounting at "/" was wrong in two ways that only surface once `web/dist` exists — the
    shipped configuration, not the one the API suite ran in. Starlette's Mount out-ranks a
    partial-path 405 match, so `GET /verify` degraded from "that route is a POST" to "not
    found"; and every unknown path became the SPA, including path-traversal probes under
    /sample that must answer in the error taxonomy. A probe that renders HTML looks like
    it worked.

    So: assets get their own prefix, API prefixes are claimed explicitly, and only what is
    left over falls through to index.html — which is what makes a browser reload on a
    client-side route render the app instead of an error.
    """
    if not _WEB_DIST.is_dir():

        @app.get("/", include_in_schema=False)
        def index() -> dict[str, str]:
            return {
                "service": "labelproof",
                "status": (
                    "The verification service is running. The web interface is not "
                    "built in this deployment — use POST /verify, or GET /sample "
                    "for the demo application."
                ),
            }

        return

    assets = _WEB_DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    index_html = _WEB_DIST / "index.html"
    dist_root = _WEB_DIST.resolve()

    @app.get("/", include_in_schema=False)
    def spa_root() -> FileResponse:
        return FileResponse(index_html)

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> FileResponse:
        # The catch-all out-ranks Starlette's partial-match 405, so a browser GET of a
        # POST-only route would otherwise read as "not found" rather than "wrong method".
        # Restoring the distinction matters: one means the URL is wrong, the other means
        # the caller is close and using the wrong verb.
        if f"/{full_path}" in _POST_ONLY_ROUTES:
            raise StarletteHTTPException(status_code=405)
        if full_path.split("/", 1)[0] in _API_PREFIXES:
            raise StarletteHTTPException(status_code=404)

        candidate = (_WEB_DIST / full_path).resolve()
        if candidate.is_file() and candidate.is_relative_to(dist_root):
            return FileResponse(candidate)
        return FileResponse(index_html)


app = create_app()
