"""The store behind reading-while-you-type (LP-346).

These are written before the endpoint, because the endpoint is the easy part. What
decides whether the design is honest is what happens to a reading nobody came back for,
and whether a token can be pointed at a label it was not taken from.
"""

from __future__ import annotations

import pytest

from api.models import Commodity, Extraction
from api.prepared import PreparedReadings, digest
from api.provider.base import ProviderUsage


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


FRONT = b"\x89PNG front bytes"
BACK = b"\x89PNG back bytes"


def a_store(**kwargs: object) -> tuple[PreparedReadings, FakeClock]:
    clock = FakeClock()
    return PreparedReadings(clock=clock, **kwargs), clock  # type: ignore[arg-type]


def stash(store: PreparedReadings, payloads: list[bytes], commodity: Commodity = Commodity.SPIRITS):
    return store.put(
        image_digest=digest(payloads),
        commodity=commodity,
        extractions=(Extraction(image_index=0),),
        reports=(),
        ingest_ms=10,
        quality_ms=5,
        extract_ms=5800,
        usage=ProviderUsage(),
        unseen=(),
    )


# --- the ordinary path ------------------------------------------------------------------


def test_a_reading_comes_back_for_the_label_it_was_taken_from() -> None:
    store, _ = a_store()
    entry = stash(store, [FRONT, BACK])

    got = store.take(entry.token, [FRONT, BACK], Commodity.SPIRITS)

    assert got is not None
    assert got.extract_ms == 5800


# --- the ways it must refuse ------------------------------------------------------------


def test_a_token_cannot_be_pointed_at_a_different_label() -> None:
    """THE ONE THAT MATTERS. A token names a reading of specific bytes. If it could be
    attached to another submission, this optimisation would be a way to have one label's
    verdict answer for another label's artwork — a false pass built out of a cache."""
    store, _ = a_store()
    entry = stash(store, [FRONT])

    assert store.take(entry.token, [BACK], Commodity.SPIRITS) is None


def test_the_order_of_the_images_is_part_of_the_label() -> None:
    """Front-then-back and back-then-front are different submissions: roles are assigned
    by index, so reusing one reading for the other would put the warning on the wrong
    picture in the evidence panel."""
    store, _ = a_store()
    entry = stash(store, [FRONT, BACK])

    assert store.take(entry.token, [BACK, FRONT], Commodity.SPIRITS) is None


def test_a_reading_taken_under_one_commodity_does_not_answer_another() -> None:
    """The commodity travels in the prompt as the rule text the model is told to apply.
    A reading taken under spirits is not the same reading relabelled — it is a different
    question that happened to be asked of the same picture."""
    store, _ = a_store()
    entry = stash(store, [FRONT], Commodity.SPIRITS)

    assert store.take(entry.token, [FRONT], Commodity.WINE) is None


def test_an_unknown_token_is_refused_rather_than_raising() -> None:
    """The caller's fallback is to extract normally, so this has to be an answer rather
    than an exception — a restart between prepare and verify is an ordinary event."""
    store, _ = a_store()
    assert store.take("not-a-real-token", [FRONT], Commodity.SPIRITS) is None


def test_a_reading_expires() -> None:
    store, clock = a_store(ttl_seconds=600)
    entry = stash(store, [FRONT])

    clock.advance(599)
    assert store.take(entry.token, [FRONT], Commodity.SPIRITS) is not None

    entry = stash(store, [FRONT])
    clock.advance(601)
    assert store.take(entry.token, [FRONT], Commodity.SPIRITS) is None


def test_a_reading_answers_one_submission_and_is_then_gone() -> None:
    """Single use. Leaving it available would keep label artwork in memory for a request
    that has already been answered, and no flow needs the same token twice."""
    store, _ = a_store()
    entry = stash(store, [FRONT])

    assert store.take(entry.token, [FRONT], Commodity.SPIRITS) is not None
    assert store.take(entry.token, [FRONT], Commodity.SPIRITS) is None


def test_a_refused_token_is_also_consumed() -> None:
    """A token offered against the wrong label is spent. Otherwise a caller could probe
    one token against many labels, and the store would hold the artwork throughout."""
    store, _ = a_store()
    entry = stash(store, [FRONT])

    assert store.take(entry.token, [BACK], Commodity.SPIRITS) is None
    assert store.take(entry.token, [FRONT], Commodity.SPIRITS) is None


# --- the ways it must not grow ----------------------------------------------------------


def test_the_store_is_bounded_and_drops_the_oldest_first() -> None:
    """`/prepare` accepts an upload and does work, so it can be called in a loop. The rate
    limiter bounds the calls per minute; nothing but this bounds the memory."""
    store, clock = a_store(max_entries=3)
    tokens = []
    for i in range(5):
        clock.advance(1)
        tokens.append(stash(store, [FRONT + bytes([i])]).token)

    assert len(store) <= 3
    # The first two are gone; the last three answer.
    assert store.take(tokens[0], [FRONT + bytes([0])], Commodity.SPIRITS) is None
    assert store.take(tokens[4], [FRONT + bytes([4])], Commodity.SPIRITS) is not None


def test_expired_readings_are_dropped_without_anyone_asking_for_them() -> None:
    """A browser closed mid-form leaves a reading nobody will ever claim. Nothing sweeps
    this store on a timer, so the write path has to do it, or artwork accumulates until
    the process restarts."""
    store, clock = a_store(ttl_seconds=60)
    for i in range(3):
        stash(store, [FRONT + bytes([i])])
    assert len(store) == 3

    clock.advance(61)
    stash(store, [BACK])

    assert len(store) == 1


@pytest.mark.parametrize("payloads", [[FRONT], [FRONT, BACK], [BACK, FRONT, FRONT]])
def test_the_digest_names_exactly_these_bytes_in_this_order(payloads: list[bytes]) -> None:
    assert digest(payloads) == digest(list(payloads))
    assert digest(payloads) != digest([*payloads, b"extra"])
