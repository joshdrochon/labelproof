"""Image and extraction pipeline: ingest, quality gate, and multi-image merge.

Ordered by the path a request takes. `api.pipeline` may import from `api.rules`; the
reverse is forbidden — the rules engine stays pure and unit-testable in milliseconds
(pinned build decision).
"""
