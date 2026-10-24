#!/bin/bash
# Structured-protocol Agent simulator for local debugging.
# Usage: bash tests/fake_agent.sh TOKEN [PANEL_URL] [SECONDS] [SIGN_KEY]
set -u
PANEL_URL="${2:-http://127.0.0.1:8000}"
TOKEN="${1:?usage: fake_agent.sh TOKEN [PANEL_URL] [SECONDS] [SIGN_KEY]}"
DURATION="${3:-90}"
SIGN_KEY="${4:-}"
SPOOL="$(mktemp -d)"
trap 'rm -rf "$SPOOL"' EXIT

heartbeat() {
  curl -fsS -m 20 -X POST "$PANEL_URL/api/agent/heartbeat" \
    -H "X-Agent-Token: $TOKEN" -H 'X-Agent-Protocol: 2' \
    -H "X-Agent-Host: fake-$(hostname)" 2>/dev/null
}

send_output() {
  curl -fsS -m 20 -X POST "$PANEL_URL/api/agent/output" \
    -H "X-Agent-Token: $TOKEN" \
    --data-urlencode "job_id=$1" \
    --data-urlencode "text@$2" \
    --data-urlencode "done=$3" \
    --data-urlencode "exit_code=$4" >/dev/null 2>&1
}

echo "fake agent token=${TOKEN:0:8}... ${DURATION}s"
end=$((SECONDS + DURATION))
while [ "$SECONDS" -lt "$end" ]; do
  resp="$(heartbeat)" || { sleep 5; continue; }
  job_id="$(printf '%s\n' "$resp" | sed -n 's/^job_id=//p')"
  [ -n "$job_id" ] || { sleep 3; continue; }
  sig="$(printf '%s\n' "$resp" | sed -n 's/^sig=//p')"
  payload_b64="$(printf '%s\n' "$resp" | sed -n 's/^payload_b64=//p')"

  if [ -n "$SIGN_KEY" ]; then
    calc="$(printf '%s' "$job_id:$payload_b64" | openssl dgst -sha256 -hmac "$SIGN_KEY" 2>/dev/null | awk '{print $NF}')"
    if [ "$calc" != "$sig" ]; then
      echo "  [fake] reject #$job_id: bad signature"
      sleep 5
      continue
    fi
  fi

  printf '%s' "$payload_b64" | base64 -d >"$SPOOL/payload"
  kind="$(sed -n 's/^kind=//p' "$SPOOL/payload" | head -n1)"
  out="$SPOOL/out"
  case "$kind" in
    report_ips)
      printf 'IPV4=93.184.216.34\nIPV6=2606:4700:4700::1111\n' >"$out" ;;
    ping)
      host="$(sed -n 's/^host=//p' "$SPOOL/payload" | head -n1)"
      count="$(sed -n 's/^count=//p' "$SPOOL/payload" | head -n1)"
      ping -c "$count" -i 1 "$host" >"$out" 2>&1
      ;;
    ensure_tools)
      command -v iperf3 >/dev/null && command -v ping >/dev/null
      echo "tools checked" >"$out"
      ;;
    *)
      printf 'structured job accepted: %s\n' "$kind" >"$out"
      ;;
  esac
  code=$?
  send_output "$job_id" "$out" 1 "$code"
  echo "  [fake] job #$job_id kind=$kind exit=$code"
  sleep 1
done
