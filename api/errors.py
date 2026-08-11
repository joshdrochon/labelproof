"""Error taxonomy (OPS-5).

Four kinds, and the split is by *who can act*:

* `user` — the request was wrong. The user fixes it.
* `image` — the artwork cannot be verified. The agent requests a better image.
* `provider` — the AI service is unreachable or misbehaving. Nobody local can fix it;
  the app degrades and says so (NET-3, TC-21).
* `internal` — we have a bug.

Every error carries a message written for a compliance agent, not an engineer: what
happened, and what to do next (UX-6). No stack traces, no "inference failed", no jargon.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorKind(StrEnum):
    USER = "user"
    IMAGE = "image"
    PROVIDER = "provider"
    INTERNAL = "internal"


#: Default HTTP status per kind. Provider trouble is 503 rather than 500 — it is not our
#: bug, and the distinction matters to anyone reading logs or a status page.
#:
#: A *default*, not the whole story. `kind` groups by who can act, which is the right
#: axis for choosing what to say to an agent and the wrong one for choosing a status
#: line: 404, 405, 413 and 429 are all "the caller can fix this", and collapsing them to
#: 400 loses information every HTTP participant downstream relies on. A 429 answering
#: 400 is not retried by any client honouring `Retry-After`; a proxy or WAF cannot tell
#: a missing route from a malformed body; and a 4xx dashboard loses the one split that
#: says whether callers are lost or the service is shedding load.
#:
#: The app already made this decision and then undid it: `main._install_spa` raises
#: `HTTPException(405)` specifically to preserve "wrong verb, not wrong URL", with a
#: comment explaining why, and the handler three lines later threw the status away. So
#: an error may now carry its own, and `kind` keeps doing the job it is good at.
_STATUS: dict[ErrorKind, int] = {
    ErrorKind.USER: 400,
    ErrorKind.IMAGE: 422,
    ErrorKind.PROVIDER: 503,
    ErrorKind.INTERNAL: 500,
}


class LabelProofError(Exception):
    """Base for every error the app raises deliberately."""

    kind: ErrorKind = ErrorKind.INTERNAL
    code: str = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        next_step: str = "",
        code: str | None = None,
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.next_step = next_step
        self._status_code = status_code
        if code:
            self.code = code

    @property
    def status_code(self) -> int:
        """The status this error goes out with.

        An explicit one wins over the kind default, so a 404 can stay a 404 while still
        being a `user` error in the body an agent reads.
        """
        return self._status_code if self._status_code is not None else _STATUS[self.kind]

    def to_payload(self) -> dict[str, object]:
        return {
            "error": {
                "kind": self.kind.value,
                "code": self.code,
                "message": self.message,
                "next_step": self.next_step or None,
            }
        }


class UserError(LabelProofError):
    kind = ErrorKind.USER
    code = "invalid_request"


class ImageError(LabelProofError):
    """The artwork cannot be verified. Mirrors the agents' existing workflow verb."""

    kind = ErrorKind.IMAGE
    code = "unreadable_image"

    def __init__(self, message: str, *, next_step: str = "retake", code: str | None = None):
        super().__init__(message, next_step=next_step, code=code)


class ProviderUnavailable(LabelProofError):
    kind = ErrorKind.PROVIDER
    code = "provider_unavailable"

    def __init__(
        self,
        message: str = (
            "The label reading service is not responding right now. Nothing has been "
            "checked. Try again in a moment — no application data has been changed."
        ),
        *,
        next_step: str = "retry",
    ):
        super().__init__(message, next_step=next_step)


class InternalError(LabelProofError):
    kind = ErrorKind.INTERNAL
    code = "internal_error"

    def __init__(
        self,
        message: str = (
            "Something went wrong on our side. Nothing has been checked and no "
            "application data has been changed."
        ),
    ):
        super().__init__(message, next_step="retry")


# Ready-made errors for the cases the PRD names by hand.

def file_too_large(limit_mb: int) -> UserError:
    return UserError(
        f"That image is larger than {limit_mb} MB. Save it at a smaller size and "
        f"upload it again.",
        next_step="resize",
        code="file_too_large",
    )


def unsupported_file_type(kind: str) -> UserError:
    return UserError(
        f"That file is a {kind}, which this tool cannot read. Upload a JPEG, PNG, "
        f"WebP, HEIC, or PDF of the label.",
        next_step="replace",
        code="unsupported_file_type",
    )


def not_a_label() -> ImageError:
    """TC-15 — somebody uploaded a photo of their cat."""
    return ImageError(
        "This does not look like a label. Nothing has been checked — upload the label "
        "artwork for this application.",
        next_step="replace",
        code="not_a_label",
    )


def unreadable(reason: str) -> ImageError:
    """The quality gate's plain-language retake reason (IMG-4)."""
    return ImageError(reason, next_step="retake", code="unreadable_image")
