#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
umask 077

if [[ -x "$PROJECT_DIR/.venv/bin/python" ]]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python"
else
    PYTHON="$(command -v python3 || true)"
fi

if [[ -z "$PYTHON" ]] || ! "$PYTHON" -c 'import fastapi, streamlit, uvicorn' >/dev/null 2>&1; then
    printf 'Python dependencies are missing. Install them with:\n  python3 -m pip install -r requirements.txt\n' >&2
    exit 1
fi

if [[ ! -f "$PROJECT_DIR/.env.local" ]]; then
    JWT_SECRET="$("$PYTHON" -c 'import secrets; print(secrets.token_hex(32))')"
    INGESTION_TOKEN="$("$PYTHON" -c 'import secrets; print(secrets.token_hex(32))')"
    cat > "$PROJECT_DIR/.env.local" <<EOF
SON_DATABASE_URL=sqlite:///./son-local.db
SON_JWT_SECRET=$JWT_SECRET
SON_INGESTION_API_TOKEN=$INGESTION_TOKEN
SON_CORS_ORIGINS=http://127.0.0.1:8501
SON_NETCONF_ENABLED=false
SON_API_URL=http://127.0.0.1:8000
EOF
    printf 'Created private local configuration: %s/.env.local\n' "$PROJECT_DIR"
fi

set -a
while IFS='=' read -r key value || [[ -n "$key" ]]; do
    case "$key" in
        ''|\#*) continue ;;
    esac
    if [[ ! "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        printf 'Invalid environment variable name in .env.local: %s\n' "$key" >&2
        exit 1
    fi
    export "$key=$value"
done < "$PROJECT_DIR/.env.local"
set +a

"$PYTHON" -m src.cli init-local
chmod 600 "$PROJECT_DIR/.env.local" "$PROJECT_DIR/son-local.db"

"$PYTHON" -m uvicorn src.api:app --host 127.0.0.1 --port 8000 &
API_PID=$!
UI_PID=

cleanup() {
    trap - EXIT INT TERM
    if kill -0 "$API_PID" 2>/dev/null; then
        kill "$API_PID"
    fi
    if [[ -n "$UI_PID" ]] && kill -0 "$UI_PID" 2>/dev/null; then
        kill "$UI_PID"
    fi
    wait "$API_PID" 2>/dev/null || true
    if [[ -n "$UI_PID" ]]; then
        wait "$UI_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

API_READY=false
for ((attempt = 0; attempt < 30; attempt++)); do
    if ! kill -0 "$API_PID" 2>/dev/null; then
        printf 'API exited before becoming ready; see the server error above.\n' >&2
        exit 1
    fi
    if "$PYTHON" -c \
        'from urllib.request import urlopen; urlopen("http://127.0.0.1:8000/health/ready", timeout=1)' \
        >/dev/null 2>&1; then
        API_READY=true
        break
    fi
    sleep 1
done

if [[ "$API_READY" != true ]]; then
    printf 'API did not become ready within 30 seconds.\n' >&2
    exit 1
fi

"$PYTHON" -m streamlit run app.py \
    --server.address 127.0.0.1 \
    --server.port 8501 \
    --server.headless true &
UI_PID=$!

printf '\nSON dashboard: http://127.0.0.1:8501\nAPI readiness: http://127.0.0.1:8000/health/ready\nPress Ctrl+C to stop both services.\n\n'
wait -n "$API_PID" "$UI_PID"
