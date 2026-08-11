#!/usr/bin/env bash
#
# Post-deploy smoke test (LP-135) — and, because CI runs it immediately after `fly
# deploy`, the trigger for automatic rollback (LP-131).
#
# This is not a health check. `/health` and `/ready` are wired into the platform and run
# every fifteen seconds; if that were sufficient this file would not exist. What it adds
# is the thing a status code cannot express: that the deployed service actually verified a
# real label, over HTTPS, against the live model, and returned a complete verdict.
#
# The failures it is built to catch, each of which passes a naive health check:
#
#   - The service answers 200 in **sample mode**, replaying built-in fixtures. Every
#     verdict is a demonstration and nothing on screen says so (LP-132).
#   - `ANTHROPIC_API_KEY` was never set, or was set on the wrong app. `/ready` catches
#     this; `/verify` proves it.
#   - The SPA did not make it into the image, so the URL serves a JSON banner and the
#     grader concludes the app is broken.
#   - HTTPS or HSTS is not actually in force at the edge (SEC-6, LP-083).
#   - The image built, booted, passed its checks — and returns four fields instead of
#     seven, because a fixture the demo needs was excluded from it.
#
# Every assertion is one that, if it failed in front of a reviewer, would end the
# evaluation. Nothing here is a style check.
#
# Usage:
#     scripts/smoke.sh https://labelproof.fly.dev
#     scripts/smoke.sh                     # defaults to $SMOKE_BASE_URL
#
# Requires: bash, curl, jq. Exit code 0 means the release is good.

set -Eeuo pipefail

BASE_URL="${1:-${SMOKE_BASE_URL:-}}"

# A single sample is a weak latency measurement, so the budget is reported and the hard
# ceiling is generous. PERF-6's ≤5s first hit is verified properly by the 20-run p95
# table, not here — rolling a release back because one request took 5.2s would cost more
# availability than the 200 ms it was defending.
BUDGET_MS="${SMOKE_BUDGET_MS:-5000}"
CEILING_MS="${SMOKE_CEILING_MS:-20000}"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

FAILURES=0

pass() { printf '  \033[32mok\033[0m    %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$1"; }
step() { printf '\n\033[1m%s\033[0m\n' "$1"; }

require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "smoke: '$1' is required but not installed." >&2
    exit 2
  }
}

require curl
require jq

if [[ -z "$BASE_URL" ]]; then
  echo "smoke: no URL. Usage: scripts/smoke.sh https://labelproof.fly.dev" >&2
  exit 2
fi
BASE_URL="${BASE_URL%/}"

printf '\033[1mLabelProof smoke test\033[0m  →  %s\n' "$BASE_URL"

# ======================================================================================
step "1. Transport (SEC-6, LP-083, LP-133)"
# ======================================================================================

if [[ "$BASE_URL" == https://* ]]; then
  pass "target is https"

  # Fly's force_https redirects; anything that answers 200 on port 80 is serving the app
  # in the clear.
  http_url="http://${BASE_URL#https://}"
  redirect_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$http_url" || echo 000)"
  if [[ "$redirect_code" =~ ^30[0-8]$ ]]; then
    pass "plain http redirects (${redirect_code})"
  else
    fail "plain http answered ${redirect_code}, expected a redirect"
  fi

  headers="$WORK_DIR/headers.txt"
  curl -sS -D "$headers" -o /dev/null --max-time 20 "$BASE_URL/health" || true

  if grep -qi '^strict-transport-security:' "$headers"; then
    hsts="$(grep -i '^strict-transport-security:' "$headers" | tr -d '\r')"
    pass "HSTS present — ${hsts#*: }"
  else
    fail "no Strict-Transport-Security header"
  fi

  for header in "x-content-type-options" "x-frame-options" "content-security-policy"; do
    if grep -qi "^${header}:" "$headers"; then
      pass "${header} present"
    else
      warn "${header} missing (edge headers may not have propagated yet)"
    fi
  done
else
  warn "target is not https — transport checks skipped (local run?)"
fi

# ======================================================================================
step "2. Liveness and readiness (NET-5, LP-132)"
# ======================================================================================

health="$WORK_DIR/health.json"
health_code="$(curl -sS -o "$health" -w '%{http_code}' --max-time 20 "$BASE_URL/health" || echo 000)"
if [[ "$health_code" == "200" ]]; then
  pass "/health 200"
else
  fail "/health returned ${health_code}"
fi

ready="$WORK_DIR/ready.json"
ready_code="$(curl -sS -o "$ready" -w '%{http_code}' --max-time 30 "$BASE_URL/ready" || echo 000)"
if [[ "$ready_code" == "200" ]]; then
  pass "/ready 200"
else
  fail "/ready returned ${ready_code} — $(jq -r '.error.message // "no body"' "$ready" 2>/dev/null)"
fi

# The assertion this whole file exists for.
#
# `/ready` reports `simulated: true` when the server is running against recorded fixtures
# instead of the model. It still answers 200, so every status-code check on earth calls
# that healthy. It is not: a deployed instance in sample mode cannot read an uploaded
# photo, and hands a compliance reviewer demonstration verdicts that are indistinguishable
# from real ones. That is a failed deployment and it must roll back.
simulated="$(jq -r '.simulated // "absent"' "$ready" 2>/dev/null || echo absent)"
case "$simulated" in
  false)
    pass "provider is live — $(jq -r '.provider // "?"' "$ready") / $(jq -r '.model // "?"' "$ready")"
    ;;
  true)
    fail "SAMPLE MODE IN PRODUCTION — verdicts here are demonstrations, not checks"
    ;;
  *)
    fail "/ready did not report a 'simulated' field; cannot confirm the provider is real"
    ;;
esac

# ======================================================================================
step "3. The single-page app is actually served"
# ======================================================================================

root="$WORK_DIR/root.html"
root_code="$(curl -sS -o "$root" -w '%{http_code}' --max-time 20 "$BASE_URL/" || echo 000)"
if [[ "$root_code" == "200" ]] && grep -qi '<div id="root"' "$root"; then
  pass "/ serves the app shell"
elif [[ "$root_code" == "200" ]]; then
  fail "/ answered 200 but is not the SPA — the web build is missing from the image"
else
  fail "/ returned ${root_code}"
fi

# A client-side route must render the app, not a 404 — the first thing that breaks when a
# reviewer reloads the page mid-flow. (`/verify-now` is a client route: it is not one of
# the API prefixes, so it falls through to index.html.)
deep_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "$BASE_URL/verify-now" || echo 000)"
if [[ "$deep_code" == "200" ]]; then
  pass "client-side route falls back to the app"
else
  fail "client-side route returned ${deep_code}, expected 200"
fi

# The converse: an unknown path under an API prefix must answer in the error taxonomy,
# not render HTML. A probe that gets a page back looks to the prober like it worked.
api_404="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "$BASE_URL/sample/nope" || echo 000)"
if [[ "$api_404" == "404" || "$api_404" == "400" ]]; then
  pass "unknown API path answers in the error taxonomy (${api_404})"
else
  fail "unknown API path returned ${api_404}, expected 4xx"
fi

# ======================================================================================
step "4. One-click sample loads (UX-1, LP-088)"
# ======================================================================================

sample="$WORK_DIR/sample.json"
sample_code="$(curl -sS -o "$sample" -w '%{http_code}' --max-time 20 "$BASE_URL/sample" || echo 000)"
if [[ "$sample_code" != "200" ]]; then
  fail "/sample returned ${sample_code} — the one-click demo is broken"
  echo; echo "smoke: ${FAILURES} failure(s). Cannot continue without the sample."; exit 1
fi
pass "/sample 200"

jq -c '.application' "$sample" > "$WORK_DIR/application.json"
image_count="$(jq '.images | length' "$sample")"
if [[ "$image_count" -ge 1 ]]; then
  pass "sample offers ${image_count} label image(s)"
else
  fail "sample offers no images"
fi

curl_args=()
while IFS=$'\t' read -r name url; do
  target="$WORK_DIR/$name"
  code="$(curl -sS -o "$target" -w '%{http_code}' --max-time 30 "${BASE_URL}${url}" || echo 000)"
  if [[ "$code" == "200" && -s "$target" ]]; then
    pass "fetched ${name} ($(wc -c < "$target" | tr -d ' ') bytes)"
    # Filename is preserved: it is how fixture replay identifies a label, so a sample-mode
    # server would answer correctly here and be caught by the /ready assertion instead of
    # by a confusing 503.
    curl_args+=(-F "images=@${target};type=image/png;filename=${name}")
  else
    fail "could not fetch sample image ${name} (${code})"
  fi
done < <(jq -r '.images[] | [.filename, .url] | @tsv' "$sample")

# ======================================================================================
step "5. A real verification against production (LP-135)"
# ======================================================================================

if [[ ${#curl_args[@]} -eq 0 ]]; then
  fail "no sample images to submit"
else
  verify="$WORK_DIR/verify.json"
  # curl's own `time_total` rather than wrapping the call in `date` — `date +%s%N` is a
  # GNU extension and silently produces garbage on BSD/macOS, which would turn a latency
  # measurement into a rollback trigger on someone's laptop.
  timing="$(curl -sS -o "$verify" -w '%{http_code} %{time_total}' --max-time 60 \
    -X POST "$BASE_URL/verify" \
    "${curl_args[@]}" \
    -F "application=<${WORK_DIR}/application.json" || echo "000 0")"

  verify_code="${timing%% *}"
  elapsed_ms="$(awk -v t="${timing##* }" 'BEGIN { printf "%d", t * 1000 }')"

  if [[ "$verify_code" != "200" ]]; then
    fail "/verify returned ${verify_code} — $(jq -r '.error.message // "no body"' "$verify" 2>/dev/null)"
  else
    pass "/verify 200"

    recommendation="$(jq -r '.aggregate.recommendation // empty' "$verify")"
    if [[ -n "$recommendation" ]]; then
      pass "aggregate recommendation: ${recommendation}"
    else
      fail "no aggregate recommendation in the response"
    fi

    # Seven mandatory label elements. Fewer means a field silently dropped out of the
    # pipeline — the kind of regression that reads as a clean pass.
    field_count="$(jq '.fields | length' "$verify")"
    if [[ "$field_count" -eq 7 ]]; then
      pass "all 7 fields returned"
    else
      fail "expected 7 fields, got ${field_count}"
    fi

    # Every verdict must carry its rationale. An unexplained verdict is the thing the
    # PRD says this product must never produce.
    unexplained="$(jq '[.fields[] | select((.rationale // "") == "")] | length' "$verify")"
    if [[ "$unexplained" -eq 0 ]]; then
      pass "every field carries a rationale"
    else
      fail "${unexplained} field(s) returned without a rationale"
    fi

    server_ms="$(jq -r '.timings_ms.total // 0' "$verify")"
    if [[ "$server_ms" -gt 0 ]]; then
      pass "server-reported total: ${server_ms} ms"
    else
      fail "no server-side timing in the response (OPS-1)"
    fi

    if [[ "$elapsed_ms" -gt 0 ]]; then
      if [[ "$elapsed_ms" -le "$BUDGET_MS" ]]; then
        pass "wall clock ${elapsed_ms} ms (budget ${BUDGET_MS} ms)"
      elif [[ "$elapsed_ms" -le "$CEILING_MS" ]]; then
        warn "wall clock ${elapsed_ms} ms exceeds the ${BUDGET_MS} ms budget — not a rollback trigger on one sample, but the p95 run will show it"
      else
        fail "wall clock ${elapsed_ms} ms is past the ${CEILING_MS} ms ceiling — the service is not usable"
      fi
    fi
  fi
fi

# ======================================================================================
printf '\n'
if [[ "$FAILURES" -eq 0 ]]; then
  printf '\033[32msmoke: release is good.\033[0m\n'
  exit 0
fi
printf '\033[31msmoke: %d failure(s) — this release must not serve traffic.\033[0m\n' "$FAILURES"
exit 1
