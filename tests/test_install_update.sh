#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

git init --bare -q -b main "$TMP/remote.git"
git init -q -b main "$TMP/seed"
git -C "$TMP/seed" -c user.name=test -c user.email=test@example.com \
  -c commit.gpgsign=false commit --allow-empty -q -m init
printf 'remote-v1\n' >"$TMP/seed/app.txt"
git -C "$TMP/seed" add app.txt
git -C "$TMP/seed" -c user.name=test -c user.email=test@example.com \
  -c commit.gpgsign=false commit -q -m v1
git -C "$TMP/seed" remote add origin "$TMP/remote.git"
git -C "$TMP/seed" push -q -u origin main

git clone -q "$TMP/remote.git" "$TMP/work"
printf 'local-change\n' >>"$TMP/work/app.txt"
printf 'untracked-change\n' >"$TMP/work/local-only.txt"
printf 'PANEL_PORT=23456\nPANEL_DOMAIN=panel.example.com\n' >"$TMP/work/.env"
printf 'custom-caddy-config\n' >"$TMP/work/Caddyfile.local"
mkdir -p "$TMP/work/certs" "$TMP/work/data"
printf 'origin-cert\n' >"$TMP/work/certs/origin.pem"
printf 'runtime-db\n' >"$TMP/work/data/panel.db"

printf 'remote-v2\n' >"$TMP/seed/app.txt"
git -C "$TMP/seed" add app.txt
git -C "$TMP/seed" -c user.name=test -c user.email=test@example.com \
  -c commit.gpgsign=false commit -q -m v2
git -C "$TMP/seed" push -q

sed -n '/^# --- update helpers/,/^# --- end update helpers/p' "$ROOT/install.sh" \
  >"$TMP/helpers.sh"

INSTALL_DIR="$TMP/work"
BRANCH=main
LOG_FILE="$TMP/update.log"
warn() { printf '%s\n' "$*" >&2; }
log_file() { printf '%s\n' "$*" >>"$LOG_FILE"; }
# shellcheck disable=SC1091
. "$TMP/helpers.sh"

git -C "$INSTALL_DIR" fetch -q origin "$BRANCH"
align_repo_to_remote

test "$(git -C "$INSTALL_DIR" rev-parse HEAD)" = \
     "$(git -C "$INSTALL_DIR" rev-parse "origin/$BRANCH")"
test "$(cat "$INSTALL_DIR/app.txt")" = "remote-v2"
test -z "$(git -C "$INSTALL_DIR" status --porcelain)"
test "$(cat "$INSTALL_DIR/.env")" = "PANEL_PORT=23456
PANEL_DOMAIN=panel.example.com"
test "$(cat "$INSTALL_DIR/Caddyfile.local")" = "custom-caddy-config"
test "$(cat "$INSTALL_DIR/certs/origin.pem")" = "origin-cert"
test "$(cat "$INSTALL_DIR/data/panel.db")" = "runtime-db"
test -n "$LOCAL_BACKUP"
test "$(git -C "$INSTALL_DIR" stash list | wc -l)" -eq 1
git -C "$INSTALL_DIR" show "$LOCAL_BACKUP:app.txt" | grep -q 'local-change'
git -C "$INSTALL_DIR" show "$LOCAL_BACKUP^3:local-only.txt" | grep -q 'untracked-change'
! git -C "$INSTALL_DIR" cat-file -e "$LOCAL_BACKUP^3:.env" 2>/dev/null
! git -C "$INSTALL_DIR" cat-file -e "$LOCAL_BACKUP^3:certs/origin.pem" 2>/dev/null

# 模拟旧版脚本已经把运行时文件 stash 走：新版启动时必须自动找回。
git clone -q "$TMP/remote.git" "$TMP/legacy"
printf 'PANEL_PORT=11192\nPANEL_BIND=127.0.0.1\nPANEL_DOMAIN=panel.example.com\n' \
  >"$TMP/legacy/.env"
printf 'legacy-caddy\n' >"$TMP/legacy/Caddyfile.local"
mkdir -p "$TMP/legacy/certs"
printf 'legacy-cert\n' >"$TMP/legacy/certs/origin.pem"
git -C "$TMP/legacy" -c user.name=test -c user.email=test@example.com \
  -c commit.gpgsign=false stash push -u -q -m "linkpilot auto-update backup test"
LOCAL_BACKUP="$(git -C "$TMP/legacy" rev-parse refs/stash)"
rm -f "$TMP/legacy/.env" "$TMP/legacy/Caddyfile.local"
rm -rf "$TMP/legacy/certs"
INSTALL_DIR="$TMP/legacy"
restore_runtime_from_backup
grep -qx 'PANEL_PORT=11192' "$INSTALL_DIR/.env"
grep -qx 'PANEL_BIND=127.0.0.1' "$INSTALL_DIR/.env"
test "$(cat "$INSTALL_DIR/Caddyfile.local")" = "legacy-caddy"
test "$(cat "$INSTALL_DIR/certs/origin.pem")" = "legacy-cert"

printf 'install.sh dirty-worktree update OK\n'
