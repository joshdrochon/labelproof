# Timed run

| | |
|---|---|
| URL | `https://labelproof.fly.dev` |
| Started | 2026-08-12 17:21:45Z |
| Runs | 20 requested, 20 succeeded |
| Server mode | ready |
| Model reported | claude-sonnet-5 |
| PERF-1 target | 5000ms |
| Enforced deadline | 20700ms |
| Payload | 2 image(s), 73 KB total |
| Commit | `9b04ed7` |
| Note | fly iad, shared-cpu-2x, auto_stop off, keepwarm on, warm |

Run 1 is labelled **first-hit**, not *cold*: it is the first request this script made, which is only a genuine cold start if the server had just started or had scaled to zero. Record what you actually arranged in `Note`.

## Summary

| Measure | n | min | p50 | p90 | p95 | p99 | max |
|---|--:|--:|--:|--:|--:|--:|--:|
| client stopwatch (ms) | 20 | 7833 | 8557 | 9550 | 9614 | 9914 | 9914 |
| server-reported total (ms) | 20 | 7630 | 8298 | 9355 | 9413 | 9696 | 9696 |

**client stopwatch** is submit-to-response measured by this script: upload, network, server work, and serialisation. It is the number a person with a stopwatch sees, minus render. **server-reported total** is the server's own `timings_ms.total` from the same requests.

PERF-1 gate: observed p95 is **9614ms** against a 5000ms target — **OVER TARGET**

All 20 successful responses contained a real verification.

Clock check: the server's reported total is below the client stopwatch on every run, by 174 to 348ms. That gap is upload, network and serialisation, and it is why the screen shows the client's number rather than the server's — the screen must never report less time than passed (PERF-2).

## Every run

| # | | HTTP | client ms | server ms | overhead | preprocess | extract | compare | recommendation | request id |
|--:|---|--:|--:|--:|--:|--:|--:|--:|---|---|
| 1 | first-hit | 200 | 8710 | 8413 | 297 | 428 | 7981 | 1 | ready_to_approve | `req_e7d1281e6e6e421d` |
| 2 | warm | 200 | 9914 | 9696 | 218 | 604 | 9087 | 1 | ready_to_approve | `req_8a91b4847558454f` |
| 3 | warm | 200 | 8073 | 7882 | 191 | 412 | 7466 | 1 | ready_to_approve | `req_2cd42a6f31174748` |
| 4 | warm | 200 | 9088 | 8779 | 309 | 412 | 8363 | 1 | ready_to_approve | `req_9a97513d466043ef` |
| 5 | warm | 200 | 8400 | 8158 | 242 | 578 | 7576 | 1 | ready_to_approve | `req_579f6f16a92d401c` |
| 6 | warm | 200 | 7947 | 7757 | 190 | 416 | 7336 | 1 | ready_to_approve | `req_cac95db0b95e4d2a` |
| 7 | warm | 200 | 8064 | 7830 | 234 | 400 | 7426 | 1 | ready_to_approve | `req_20bc6eb873ad457d` |
| 8 | warm | 200 | 8337 | 8158 | 179 | 572 | 7581 | 2 | ready_to_approve | `req_99ec74201fa5420e` |
| 9 | warm | 200 | 8833 | 8535 | 298 | 590 | 7940 | 2 | ready_to_approve | `req_45c39b2b74414753` |
| 10 | warm | 200 | 7868 | 7668 | 200 | 558 | 7104 | 1 | ready_to_approve | `req_a9b57f3a3e984907` |
| 11 | warm | 200 | 7929 | 7716 | 213 | 586 | 7125 | 1 | ready_to_approve | `req_399482f0400e4b19` |
| 12 | warm | 200 | 9614 | 9413 | 201 | 396 | 9012 | 1 | ready_to_approve | `req_d3f4c68824e3438e` |
| 13 | warm | 200 | 9327 | 9007 | 320 | 399 | 8604 | 1 | ready_to_approve | `req_b0bba069268c4f55` |
| 14 | warm | 200 | 8957 | 8769 | 188 | 413 | 8351 | 1 | ready_to_approve | `req_b97e3a526c844470` |
| 15 | warm | 200 | 8696 | 8502 | 194 | 586 | 7911 | 2 | ready_to_approve | `req_eeffe4821cc9484b` |
| 16 | warm | 200 | 7833 | 7630 | 203 | 411 | 7216 | 1 | ready_to_approve | `req_3a7a1eba8b4e4bd8` |
| 17 | warm | 200 | 8120 | 7772 | 348 | 403 | 7365 | 1 | ready_to_approve | `req_52e9d56cef6b4a62` |
| 18 | warm | 200 | 8557 | 8298 | 259 | 411 | 7883 | 1 | ready_to_approve | `req_6a1fd9ea2b51429b` |
| 19 | warm | 200 | 9550 | 9355 | 195 | 550 | 8800 | 2 | ready_to_approve | `req_11fad9c63fd04eb0` |
| 20 | warm | 200 | 8693 | 8519 | 174 | 567 | 7946 | 1 | ready_to_approve | `req_3fea363292cf4cf6` |

Cost across 20 priced run(s): $0.6266 total, $0.0313 mean.
