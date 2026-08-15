# Timed run

| | |
|---|---|
| URL | `https://labelproof.fly.dev` |
| Started | 2026-08-15 00:38:08Z |
| Runs | 20 requested, 20 succeeded |
| Server mode | ready |
| Model reported | claude-sonnet-5 |
| PERF-1 target | 5000ms |
| Enforced deadline | 20700ms |
| Payload | 2 image(s), 73 KB total |
| Commit | `e360dc0` |
| Note | fly iad, performance-1x, split mode, auto_stop off, keepwarm on, warm |

Run 1 is labelled **first-hit**, not *cold*: it is the first request this script made, which is only a genuine cold start if the server had just started or had scaled to zero. Record what you actually arranged in `Note`.

## Summary

| Measure | n | min | p50 | p90 | p95 | p99 | max |
|---|--:|--:|--:|--:|--:|--:|--:|
| client stopwatch (ms) | 20 | 5301 | 5662 | 6251 | 6368 | 6983 | 6983 |
| server-reported total (ms) | 20 | 5089 | 5426 | 5842 | 6070 | 6757 | 6757 |

**client stopwatch** is submit-to-response measured by this script: upload, network, server work, and serialisation. It is the number a person with a stopwatch sees, minus render. **server-reported total** is the server's own `timings_ms.total` from the same requests.

PERF-1 gate: observed p95 is **6368ms** against a 5000ms target — **OVER TARGET**

All 20 successful responses contained a real verification.

Clock check: the server's reported total is below the client stopwatch on every run, by 173 to 660ms. That gap is upload, network and serialisation, and it is why the screen shows the client's number rather than the server's — the screen must never report less time than passed (PERF-2).

## Every run

| # | | HTTP | client ms | server ms | overhead | preprocess | extract | compare | recommendation | request id |
|--:|---|--:|--:|--:|--:|--:|--:|--:|---|---|
| 1 | first-hit | 200 | 5796 | 5619 | 177 | 555 | 5059 | 1 | ready_to_approve | `req_4eae25893b9f4db1` |
| 2 | warm | 200 | 5738 | 5516 | 222 | 541 | 4970 | 1 | ready_to_approve | `req_311520c0bd5e4fa7` |
| 3 | warm | 200 | 5607 | 5134 | 473 | 551 | 4577 | 1 | ready_to_approve | `req_2deba5cacb724029` |
| 4 | warm | 200 | 5586 | 5411 | 175 | 550 | 4856 | 1 | ready_to_approve | `req_bd9d4e730e3c4eec` |
| 5 | warm | 200 | 6368 | 5708 | 660 | 549 | 5154 | 1 | ready_to_approve | `req_46744805d64c4497` |
| 6 | warm | 200 | 5892 | 5462 | 430 | 547 | 4910 | 1 | ready_to_approve | `req_fd735baaf15443e6` |
| 7 | warm | 200 | 5662 | 5426 | 236 | 550 | 4869 | 1 | ready_to_approve | `req_48271608a7ab43cd` |
| 8 | warm | 200 | 5426 | 5196 | 230 | 550 | 4642 | 1 | ready_to_approve | `req_3365dfffc04d4f07` |
| 9 | warm | 200 | 5686 | 5497 | 189 | 541 | 4948 | 3 | ready_to_approve | `req_d4144b038fc342b8` |
| 10 | warm | 200 | 5577 | 5386 | 191 | 546 | 4835 | 1 | ready_to_approve | `req_94b83a1f6fa64402` |
| 11 | warm | 200 | 6251 | 6070 | 181 | 544 | 5521 | 1 | ready_to_approve | `req_bdc769dcf5e946c1` |
| 12 | warm | 200 | 5583 | 5410 | 173 | 548 | 4858 | 1 | ready_to_approve | `req_a119002ca08c46f5` |
| 13 | warm | 200 | 5403 | 5227 | 176 | 536 | 4686 | 1 | ready_to_approve | `req_3aa933d25b794e41` |
| 14 | warm | 200 | 6983 | 6757 | 226 | 592 | 6160 | 1 | ready_to_approve | `req_63b199981a94414e` |
| 15 | warm | 200 | 5930 | 5615 | 315 | 555 | 5055 | 1 | ready_to_approve | `req_b602af7e3c88494e` |
| 16 | warm | 200 | 5301 | 5089 | 212 | 551 | 4532 | 1 | ready_to_approve | `req_d2fbe930832b49f7` |
| 17 | warm | 200 | 6048 | 5842 | 206 | 546 | 5291 | 1 | ready_to_approve | `req_af2cfd65210f44d2` |
| 18 | warm | 200 | 5455 | 5237 | 218 | 556 | 4676 | 1 | ready_to_approve | `req_5c7776e12c8541e9` |
| 19 | warm | 200 | 5929 | 5751 | 178 | 546 | 5201 | 1 | ready_to_approve | `req_80f3b33b66b8408c` |
| 20 | warm | 200 | 5359 | 5171 | 188 | 569 | 4597 | 1 | ready_to_approve | `req_06bcad6633e844d8` |

Cost across 20 priced run(s): $0.9233 total, $0.0462 mean.
