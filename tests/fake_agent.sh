#!/bin/bash
# 模拟 agent：本地调试面板任务管道用（与 app/agent/install.sh 里的真实 agent 协议一致，含验签）
# 用法: bash tests/fake_agent.sh <接入令牌> [面板地址] [运行秒数] [签名密钥]
#       TAMPER=1 时故意破坏任务签名，用于演示"验签失败拒绝执行"
PANEL_URL="${2:-http://127.0.0.1:8000}"
TOKEN="${1:?用法: fake_agent.sh <token> [面板地址] [秒数] [签名密钥]}"
DURATION="${3:-90}"
SIGN_KEY="${4:-}"
SPOOL=$(mktemp -d)

heartbeat() {
  curl -fsS -m 20 -X POST "$PANEL_URL/api/agent/heartbeat" \
    -H "X-Agent-Token: $TOKEN" -H "X-Agent-Host: fake-$(hostname)" 2>/dev/null
}

send_output() { # $1=job_id $2=file $3=done $4=exit_code
  curl -fsS -m 20 -X POST "$PANEL_URL/api/agent/output" \
    -H "X-Agent-Token: $TOKEN" \
    --data-urlencode "job_id=$1" \
    --data-urlencode "text=$(cat "$2" 2>/dev/null)" \
    --data-urlencode "done=$3" \
    --data-urlencode "exit_code=$4" >/dev/null 2>&1
}

echo "fake agent 启动 token=${TOKEN:0:8}... 运行 ${DURATION}s 验签:$([ -n "$SIGN_KEY" ] && echo 开 || echo 关)${TAMPER:+(篡改模式)}"
end=$((SECONDS + DURATION))
while [ $SECONDS -lt $end ]; do
  resp=$(heartbeat) || { sleep 5; continue; }
  job_id=$(printf '%s\n' "$resp" | sed -n 's/^job_id=//p')
  if [ -z "$job_id" ]; then
    sleep 3
    continue
  fi
  timeout=$(printf '%s\n' "$resp" | sed -n 's/^timeout=//p')
  timeout=${timeout:-120}
  sig=$(printf '%s\n' "$resp" | sed -n 's/^sig=//p')
  cmd_b64=$(printf '%s\n' "$resp" | sed -n 's/^cmd_b64=//p')

  if [ -n "$SIGN_KEY" ]; then
    calc=$(printf '%s' "$job_id:$cmd_b64" | openssl dgst -sha256 -hmac "$SIGN_KEY" 2>/dev/null | awk '{print $NF}')
    if [ -n "$TAMPER" ]; then
      cmd_b64="${cmd_b64}A"
      calc=$(printf '%s' "$job_id:$cmd_b64" | openssl dgst -sha256 -hmac "$SIGN_KEY" 2>/dev/null | awk '{print $NF}')
    fi
    if [ "$calc" != "$sig" ]; then
      echo "  [fake] ❌ 任务 #$job_id 签名校验失败，拒绝执行（疑似篡改）"
      sleep 5
      continue
    fi
  fi

  printf '%s\n' "$cmd_b64" | base64 -d > "$SPOOL/job.sh"
  echo "  [fake] 领取任务 #$job_id"
  out="$SPOOL/job.out"
  : > "$out"
  bash "$SPOOL/job.sh" > "$out" 2>&1 &
  pid=$!
  offset=0
  start=$SECONDS
  while kill -0 "$pid" 2>/dev/null; do
    size=$(wc -c < "$out" 2>/dev/null || echo 0)
    if [ "$size" -gt "$offset" ]; then
      tail -c +"$((offset + 1))" "$out" > "$SPOOL/chunk" 2>/dev/null
      offset=$size
      send_output "$job_id" "$SPOOL/chunk" 0 ""
    fi
    if [ $((SECONDS - start)) -gt $((timeout + 120)) ]; then
      kill "$pid" 2>/dev/null
    fi
    sleep 1
  done
  wait "$pid"
  exit_code=$?
  size=$(wc -c < "$out" 2>/dev/null || echo 0)
  if [ "$size" -gt "$offset" ]; then
    tail -c +"$((offset + 1))" "$out" > "$SPOOL/chunk" 2>/dev/null
    send_output "$job_id" "$SPOOL/chunk" 0 ""
  fi
  : > "$SPOOL/done"
  send_output "$job_id" "$SPOOL/done" 1 "$exit_code"
  echo "  [fake] 任务 #$job_id 完成 exit=$exit_code"
done
echo "fake agent 退出"
