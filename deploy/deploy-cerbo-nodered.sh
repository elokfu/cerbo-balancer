#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash scripts/deploy-cerbo-nodered.sh --host <cerbo-host-or-ip> --user <ssh-user>

Optional flags:
  --flow <path>            Source flow JSON (default: node-red-flows-latest.json next to this script)
  --cred <path>            Source flows_cred.json. If omitted, credentials are not changed.
  --remote-dir <path>      Remote Node-RED directory (default: /data/home/nodered)
  --service <name>         Node-RED service name (default: nodered)
  --manager <systemctl|service|sv|auto>  Node-RED service manager (default: auto)
  --dashboard-url <url>     URL to validate after deploy (default: http://localhost:1880/ui/)
  --timeout-seconds <n>    Seconds to wait before validation (default: 8)
  --dry-run                Print commands only.
  -h                       This help.
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=0
HOST=""
USER="root"
FLOW_SRC="$SCRIPT_DIR/node-red-flows-latest.json"
CRED_SRC=""
REMOTE_DIR="/data/home/nodered"
SERVICE="nodered"
MANAGER="auto"
DASHBOARD_URL="http://localhost:1880/ui/"
TIMEOUT_SECONDS=8

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      HOST="$2"; shift 2 ;;
    --user)
      USER="$2"; shift 2 ;;
    --flow)
      FLOW_SRC="$2"; shift 2 ;;
    --cred)
      CRED_SRC="$2"; shift 2 ;;
    --remote-dir)
      REMOTE_DIR="$2"; shift 2 ;;
    --service)
      SERVICE="$2"; shift 2 ;;
    --manager)
      MANAGER="$2"; shift 2 ;;
    --dashboard-url)
      DASHBOARD_URL="$2"; shift 2 ;;
    --timeout-seconds)
      TIMEOUT_SECONDS="$2"; shift 2 ;;
    --dry-run)
      DRY_RUN=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$HOST" ]]; then
  echo "ERROR: --host is required" >&2
  usage
  exit 1
fi

if [[ ! -f "$FLOW_SRC" ]]; then
  echo "ERROR: flow source missing: $FLOW_SRC" >&2
  exit 1
fi

if [[ -n "$CRED_SRC" && ! -f "$CRED_SRC" ]]; then
  echo "ERROR: credentials source missing: $CRED_SRC" >&2
  exit 1
fi

SSH_TARGET="${USER}@${HOST}"
BACKUP_DIR="/tmp/nodered-deploy-backup-$(date +%Y%m%d-%H%M%S)"
REMOTE_FLOW="$REMOTE_DIR/flows.json"
REMOTE_CRED="$REMOTE_DIR/flows_cred.json"

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "DRY-RUN: $*"
    return 0
  fi
  "$@"
}

if ! command -v ssh >/dev/null 2>&1 || ! command -v scp >/dev/null 2>&1; then
  echo "ERROR: ssh and scp required" >&2
  exit 1
fi

run ssh "$SSH_TARGET" "mkdir -p '$BACKUP_DIR' '$REMOTE_DIR'"
run ssh "$SSH_TARGET" "cp '$REMOTE_FLOW' '$BACKUP_DIR/flows.json.bak' 2>/dev/null || true"
run ssh "$SSH_TARGET" "cp '$REMOTE_CRED' '$BACKUP_DIR/flows_cred.json.bak' 2>/dev/null || true"

run scp "$FLOW_SRC" "$SSH_TARGET:$REMOTE_FLOW"
run ssh "$SSH_TARGET" "python3 - <<'PY'
import json
p='${REMOTE_FLOW}'
with open(p,'r',encoding='utf-8') as f:
    json.load(f)
print('flows.json: OK')
PY"

if [[ -n "$CRED_SRC" ]]; then
  run scp "$CRED_SRC" "$SSH_TARGET:$REMOTE_CRED"
fi

run_service_cycle() {
  local manager="$1"
  case "$manager" in
    systemctl)
      ssh "$SSH_TARGET" "systemctl stop '$SERVICE' && systemctl start '$SERVICE'"
      ;;
    service)
      ssh "$SSH_TARGET" "service '$SERVICE' stop && service '$SERVICE' start"
      ;;
    sv)
      ssh "$SSH_TARGET" "sv stop '$SERVICE' && sv start '$SERVICE'"
      ;;
    auto)
      if ssh "$SSH_TARGET" "command -v systemctl >/dev/null 2>&1"; then
        if ssh "$SSH_TARGET" "systemctl list-units --full --all '$SERVICE'.service --no-legend >/dev/null 2>&1"; then
          ssh "$SSH_TARGET" "systemctl stop '$SERVICE' && systemctl start '$SERVICE'"
          return 0
        fi
      fi
      if ssh "$SSH_TARGET" "command -v sv >/dev/null 2>&1"; then
        ssh "$SSH_TARGET" "sv stop '$SERVICE' && sv start '$SERVICE'" && return 0
      fi
      if ssh "$SSH_TARGET" "command -v service >/dev/null 2>&1"; then
        ssh "$SSH_TARGET" "service '$SERVICE' stop && service '$SERVICE' start" && return 0
      fi
      echo "ERROR: No supported service manager found (systemctl/service/sv)" >&2
      exit 1
      ;;
    *)
      echo "ERROR: Unsupported manager '$manager'" >&2
      exit 1
      ;;
  esac
}

run run_service_cycle "$MANAGER"

echo "Waiting ${TIMEOUT_SECONDS}s for Node-RED startup..."
sleep "$TIMEOUT_SECONDS"

run ssh "$SSH_TARGET" "[ -f '$REMOTE_FLOW' ]"
run ssh "$SSH_TARGET" "python3 - <<'PY'
import json
p='${REMOTE_FLOW}'
with open(p,'r',encoding='utf-8') as f:
    data=json.load(f)
if any('spy' in str(node).lower() for node in data):
    raise SystemExit('Found spy references')
print('flows.json: deployed, no spy references found')
PY"

if [[ -n "$CRED_SRC" ]]; then
  run ssh "$SSH_TARGET" "[ -f '$REMOTE_CRED' ]"
fi

if [[ -n "$DASHBOARD_URL" ]]; then
  run ssh "$SSH_TARGET" "python3 - <<'PY'
import urllib.request
url='${DASHBOARD_URL}'
with urllib.request.urlopen(url, timeout=5) as r:
    code=r.getcode()
print('dashboard status:', code)
if code >= 500:
    raise SystemExit('dashboard error status: {}'.format(code))
PY"
fi

echo "Deploy complete"
echo "Backup snapshot path on remote: $BACKUP_DIR"
echo "Restore with:"
echo "  ssh ${SSH_TARGET} 'cp $BACKUP_DIR/flows.json.bak $REMOTE_FLOW'"
if [[ -n "$CRED_SRC" ]]; then
  echo "  ssh ${SSH_TARGET} 'cp $BACKUP_DIR/flows_cred.json.bak $REMOTE_CRED'"
fi
case "$MANAGER" in
  systemctl|auto)
    echo "  ssh ${SSH_TARGET} \"systemctl stop '$SERVICE' && systemctl start '$SERVICE'\""
    ;;
  service)
    echo "  ssh ${SSH_TARGET} \"service '$SERVICE' stop && service '$SERVICE' start\""
    ;;
  sv)
    echo "  ssh ${SSH_TARGET} \"sv stop '$SERVICE' && sv start '$SERVICE'\""
    ;;
esac
