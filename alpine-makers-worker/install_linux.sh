#!/usr/bin/env bash
set -euo pipefail
if [[ "${ALPINE_LOCAL_SANDBOX:-}" == "1" || -f "$(dirname "${BASH_SOURCE[0]}")/TEST_SANDBOX_ONLY.txt" ]]; then
  echo "Paquet TEST : installateur systeme desactive ; utiliser le lanceur local isole." >&2
  exit 2
fi
if [[ $# -lt 2 ]]; then echo "Usage: sudo ./install_linux.sh https://dashboard.example CODE [NAME]"; exit 2; fi
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then echo "Erreur : relance cet installateur avec sudo." >&2; exit 2; fi
INSTALL_ROOT="${ALPINE_WORKER_INSTALL_DIR:-/opt/alpine-makers-worker}"
if [[ "$INSTALL_ROOT" != /* ]]; then echo "Erreur : ALPINE_WORKER_INSTALL_DIR doit être un chemin absolu." >&2; exit 2; fi

python_is_compatible() {
  local candidate="${1:-}"
  [[ -n "$candidate" ]] && "$candidate" -c 'import sys, venv, ensurepip; raise SystemExit(0 if sys.version_info >= (3, 9) else 3)' >/dev/null 2>&1
}

git_is_compatible() {
  local candidate="${1:-}"
  [[ -n "$candidate" ]] && "$candidate" --version >/dev/null 2>&1
}

install_dependencies() {
  echo "Installation automatique de Python 3.9+ et Git..."
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y python3 python3-venv python3-pip git ca-certificates
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3 python3-pip git ca-certificates
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3 python3-pip git ca-certificates
  elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install python3 python3-pip git ca-certificates
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache python3 py3-pip py3-virtualenv git ca-certificates
  elif command -v pacman >/dev/null 2>&1; then
    pacman -Sy --needed --noconfirm python python-pip git ca-certificates
  else
    echo "Erreur : aucun gestionnaire compatible détecté (apt, dnf, yum, zypper, apk ou pacman)." >&2
    exit 3
  fi
}

PYTHON_BIN="$(command -v python3 || true)"
GIT_BIN="$(command -v git || true)"
if ! python_is_compatible "$PYTHON_BIN" || ! git_is_compatible "$GIT_BIN"; then
  install_dependencies
  hash -r
  PYTHON_BIN="$(command -v python3 || true)"
  GIT_BIN="$(command -v git || true)"
fi
if ! python_is_compatible "$PYTHON_BIN"; then
  echo "Erreur : Python 3.9 ou supérieur reste indisponible après l'installation automatique." >&2
  exit 3
fi
echo "Python détecté : $PYTHON_BIN"
if ! git_is_compatible "$GIT_BIN"; then
  echo "Erreur : Git reste indisponible après l'installation automatique." >&2
  exit 3
fi
echo "Git détecté : $GIT_BIN"

install -d -m 700 "$INSTALL_ROOT"
worker_source="$(cd "$(dirname "$0")" && pwd)"
if [[ "$worker_source" != "$INSTALL_ROOT" ]]; then
  for worker_item in "$worker_source"/* "$worker_source"/.[!.]*; do
    [[ -e "$worker_item" ]] || continue
    case "$(basename "$worker_item")" in
      config.json|storage.json|components|cache|temp|runtime|logs|data|outputs|workspace|jobs|config|supervisor.lock|.worker-detached) continue ;;
    esac
    cp -a "$worker_item" "$INSTALL_ROOT/"
  done
fi
chmod 700 "$INSTALL_ROOT/agent.py"
"$PYTHON_BIN" "$INSTALL_ROOT/storage_paths.py" --root "$INSTALL_ROOT" --initialize
"$PYTHON_BIN" "$INSTALL_ROOT/identity_setup.py"
PAIR_OPTIONS=()
if [[ "${ALPINE_WORKER_GPU_OPTIONAL:-0}" == "1" ]]; then
  PAIR_OPTIONS+=(--gpu-optional)
  echo "Association Linux sans GPU obligatoire avec le code de ton compte."
fi
if ALPINE_INSTALL_ROOT="$INSTALL_ROOT" "$PYTHON_BIN" -c 'import json,os; c=json.load(open(os.path.join(os.environ["ALPINE_INSTALL_ROOT"],"config.json"))); raise SystemExit(0 if c.get("worker_id") and c.get("token") else 1)' 2>/dev/null; then
  echo "Identité existante conservée ; migration cryptographique explicite disponible."
elif [ -n "${3:-}" ]; then
  "$PYTHON_BIN" "$INSTALL_ROOT/agent.py" --config "$INSTALL_ROOT/config.json" --pair "$1" "$2" --name "$3" "${PAIR_OPTIONS[@]}"
else
  "$PYTHON_BIN" "$INSTALL_ROOT/agent.py" --config "$INSTALL_ROOT/config.json" --pair "$1" "$2" "${PAIR_OPTIONS[@]}"
fi
cat >/etc/systemd/system/alpine-makers-worker.service <<EOF
[Unit]
Description=Alpine Makers Worker
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
ExecStart=$PYTHON_BIN $INSTALL_ROOT/agent.py --config $INSTALL_ROOT/config.json
Restart=always
RestartSec=5
RestartPreventExitStatus=64
Environment=ALPINE_WORKER_SUPERVISOR_EXIT_CODES=64
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$INSTALL_ROOT
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now alpine-makers-worker.service
echo "Worker associé et démarré automatiquement."
