#!/usr/bin/env bash
#
# LP-283 / PERF-6 — prove the deployed URL has no cold start.
#
# The failure this catches is Fly stopping an idle machine. `fly.toml` sets
# `auto_stop_machines = "off"`, but a config value is a claim; this is the measurement.
#
# WHY THE GAP IS 20 MINUTES. Fly's auto-stop fires after roughly 5 minutes of idle. A
# probe that hits every minute keeps the machine awake by hitting it — it would pass
# against a misconfigured app and prove nothing. Each gap here is four times the window
# the failure needs, so every probe below is a genuinely cold first hit.
#
# WHAT IS BEING TIMED. The first request after each gap, against a route that does no
# model work: `/healthz` costs nothing, so anything above a few hundred ms is transport
# plus boot rather than inference. A machine that had to start pays seconds, and that is
# the signal. A second hit follows immediately as the warm control — without it, a slow
# number could be the network rather than a cold start.
#
# Usage:  bash scripts/probe_keepwarm.sh <base-url> <hours> [out-file]

set -uo pipefail

BASE="${1:?usage: probe_keepwarm.sh <base-url> <hours> [out-file]}"
HOURS="${2:?usage: probe_keepwarm.sh <base-url> <hours> [out-file]}"
OUT="${3:-docs/keepwarm-probe.txt}"

GAP_SECONDS=1200 # 20 minutes — see the note above before shortening this
PROBES=$(python3 -c "print(max(1, round(${HOURS} * 3600 / ${GAP_SECONDS})))")

# `%{time_total}` rather than wall clock: it excludes process startup, so a slow number
# is the server's, not the shell's.
timed() {
	curl -s -o /dev/null -w '%{http_code} %{time_total}' --max-time 30 "$1" 2>/dev/null ||
		echo "000 timeout"
}

{
	echo "LP-283 / PERF-6 — keep-warm probe"
	echo "target:    ${BASE}"
	echo "started:   $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
	echo "plan:      ${PROBES} probes, ${GAP_SECONDS}s idle before each (4x Fly's ~5 min auto-stop window)"
	echo "route:     /healthz — no model call, so latency is transport plus boot"
	echo
	printf '%-22s  %-8s  %-10s  %-10s  %s\n' "utc" "status" "cold_s" "warm_s" "note"
} >"${OUT}"

worst=0
failures=0

for i in $(seq 1 "${PROBES}"); do
	# Idle FIRST, so probe 1 is already cold rather than landing on a machine the deploy
	# just warmed up.
	sleep "${GAP_SECONDS}"

	read -r code cold <<<"$(timed "${BASE}/healthz")"
	read -r _ warm <<<"$(timed "${BASE}/healthz")"

	note="ok"
	if [ "${code}" != "200" ]; then
		note="UNREACHABLE (${code})"
		failures=$((failures + 1))
	elif python3 -c "import sys; sys.exit(0 if ${cold} > 2.0 else 1)"; then
		note="COLD START — first hit over 2s"
		failures=$((failures + 1))
	fi

	worst=$(python3 -c "print(max(${worst}, ${cold}))")

	printf '%-22s  %-8s  %-10s  %-10s  %s\n' \
		"$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "${code}" "${cold}" "${warm}" "${note}" >>"${OUT}"
done

{
	echo
	echo "finished:  $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
	echo "probes:    ${PROBES}"
	echo "worst first-hit: ${worst}s"
	if [ "${failures}" -eq 0 ]; then
		echo "RESULT:    PASS — every cold first hit answered under 2s. No machine stopped."
	else
		echo "RESULT:    FAIL — ${failures} of ${PROBES} probes were slow or unreachable."
	fi
	echo
	echo "Scope: this proves the machine does not stop over the window probed. It does not"
	echo "prove anything about longer drift, and the window is stated above rather than"
	echo "rounded up in prose."
} >>"${OUT}"
