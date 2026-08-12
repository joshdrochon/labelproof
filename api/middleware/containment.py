"""No exception leaves this process as a traceback (SEC-4, LP-086).

`api/logging.py` allowlists field *names* and raises on anything else, so nothing can be
logged deliberately. Nothing about that governs an accident, and there is a real one:

Starlette's `ServerErrorMiddleware` calls the registered `Exception` handler and then
**re-raises unconditionally** — its own comment says it does this so servers can log the
error. uvicorn obliges, with `logger.error("Exception in ASGI application", exc_info=True)`,
and the full traceback lands on stdout. The allowlist is not on that path at all.

The payload is not hypothetical. Extraction responses are validated on receipt, and a
pydantic `ValidationError` renders `input_value=...` — which on that path is the label text
the model just read. The whole point of SEC-4 is that this text never reaches a log line.

This middleware is installed as the outermost user middleware, so it catches the exception
**before** `ServerErrorMiddleware` ever sees it. Nothing is re-raised. The agent gets the
same taxonomy 500 the app factory's handler would have produced, and the log gets one line
naming the exception class and nothing else.

`BaseException` — `KeyboardInterrupt`, `SystemExit`, `asyncio.CancelledError` — is
deliberately not caught. Cancellation in particular must propagate or the server cannot shut
down, and none of the three carries label content.
"""

from __future__ import annotations

from api import errors
from api import logging as applog
from api.middleware.asgi import (
    ASGIApp,
    Message,
    Receive,
    Scope,
    Send,
    send_error,
)


class ExceptionContainmentMiddleware:
    """Turns any unhandled exception into a scrubbed log line and a taxonomy 500."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def wrapped(message: Message) -> None:
            nonlocal started
            if message.get("type") == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, wrapped)
        except Exception as exc:  # noqa: BLE001 - outermost ASGI layer; nothing above it catches
            # This layer sits OUTSIDE the app factory's request-context middleware, so by
            # the time an exception arrives here the request ID that middleware assigns is
            # either unreachable (it runs the app in its own task) or never got assigned at
            # all. Without this, a 500 was the one response in the app with no correlation
            # ID — on precisely the response an agent is most likely to be reading out to
            # whoever can help them, and while the README promised "the ID an agent reads
            # off the screen is the ID in the logs".
            request_id = applog.current_request_id() or applog.new_request_id()

            # `reason_code` is on the logging allowlist and an exception class name is a
            # code identifier, never content — so a developer still learns what broke
            # without a single byte of the label reaching stdout.
            applog.error(
                "unhandled_exception",
                kind="internal",
                code="internal_error",
                reason_code=type(exc).__name__,
                status=500,
            )
            if started:
                # The status line is already on the wire; there is no response left to
                # replace. The client sees a truncated body, which is worse for them and
                # better than a traceback on stdout, which is worse for everyone.
                return
            await send_error(
                send,
                errors.InternalError(),
                status=500,
                extra_headers={
                    "Cache-Control": "no-store",
                    "X-Request-ID": request_id,
                },
            )
