#!/usr/bin/env bash
# Full local test pipeline:
#   clear → compose → lint → unit → integration → sim users → live/bench
#
#   ./scripts/test_local.sh
#   QUICK=1 ./scripts/test_local.sh          # no volume wipe, no live/bench
#   SKIP_LIVE=1 ./scripts/test_local.sh      # stop before live LLM
#   SKIP_SIM=1 ./scripts/test_local.sh       # skip multi-user agent simulation
#   BENCH_CASE_LIMIT=  ./scripts/test_local.sh  # empty = full LLM case matrix
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ENV_FILE="${ENV_FILE:-.env}"
QUICK="${QUICK:-0}"
SKIP_LIVE="${SKIP_LIVE:-0}"
SKIP_SIM="${SKIP_SIM:-0}"
# Cap live desk/knowledge LLM fan-out unless caller overrides (empty = unlimited).
if [[ -z "${BENCH_CASE_LIMIT+x}" ]]; then
  export BENCH_CASE_LIMIT=8
fi

PYTHON="${ROOT}/.venv/bin/python"
COMPOSE=(docker compose --env-file "$ENV_FILE")

pass_n=0
fail_n=0
skip_n=0

section() {
  printf '\n==> %s\n' "$1"
}

ok() {
  pass_n=$((pass_n + 1))
  printf 'OK   %s\n' "$1"
}

fail() {
  fail_n=$((fail_n + 1))
  printf 'FAIL %s\n' "$1" >&2
}

skip() {
  skip_n=$((skip_n + 1))
  printf 'SKIP %s\n' "$1"
}

load_env_file() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    return 0
  fi
  set -a
  # shellcheck disable=SC1090
  source "$path"
  set +a
}

preflight() {
  section "preflight"
  if [[ ! -x "$PYTHON" ]]; then
    fail "missing .venv — run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
  fi
  if ! command -v docker >/dev/null 2>&1; then
    fail "docker not found"
    exit 1
  fi
  if ! docker compose version >/dev/null 2>&1; then
    fail "docker compose not found"
    exit 1
  fi
  if [[ ! -f "$ENV_FILE" ]]; then
    fail "missing $ENV_FILE"
    exit 1
  fi
  load_env_file "$ENV_FILE"
  load_env_file ".env.llm"
  export DATABASE_URL="${DATABASE_URL:-postgresql://genai:${POSTGRES_PASSWORD:-genai}@localhost:${POSTGRES_PORT:-5432}/genai}"
  export REDIS_URL="${REDIS_URL:-redis://localhost:${REDIS_PORT:-6379}/0}"
  export QDRANT_URL="${QDRANT_URL:-http://localhost:${QDRANT_PORT:-6333}}"
  export PLATFORM_KEY_PEPPER="${PLATFORM_KEY_PEPPER:-local-pepper}"
  export PLATFORM_ADMIN_TOKEN="${PLATFORM_ADMIN_TOKEN:-local-admin}"
  export LLM_CREDENTIALS_FILE="${LLM_CREDENTIALS_FILE:-config/llm_credentials.yaml}"
  ok "env and tools ready"
}

clear_stack() {
  section "clear old data"
  if [[ "$QUICK" == "1" ]]; then
    skip "volume wipe (QUICK=1)"
    return 0
  fi
  "${COMPOSE[@]}" down -v --remove-orphans
  rm -f reports/bench.json
  ok "compose volumes removed"
}

start_stack() {
  section "compose up postgres redis qdrant"
  "${COMPOSE[@]}" up -d --build postgres redis qdrant
  ok "compose started"
}

wait_healthy() {
  section "wait healthy (90s)"
  local deadline=$((SECONDS + 90))
  local pg_ok=0 redis_ok=0 qdrant_ok=0
  while (( SECONDS < deadline )); do
    if docker compose --env-file "$ENV_FILE" exec -T postgres \
      pg_isready -U genai -d genai >/dev/null 2>&1; then
      pg_ok=1
    fi
    if docker compose --env-file "$ENV_FILE" exec -T redis \
      redis-cli ping 2>/dev/null | grep -q PONG; then
      redis_ok=1
    fi
    if curl -fsS "${QDRANT_URL}/readyz" >/dev/null 2>&1 \
      || curl -fsS "${QDRANT_URL}/" >/dev/null 2>&1; then
      qdrant_ok=1
    fi
    if (( pg_ok == 1 && redis_ok == 1 && qdrant_ok == 1 )); then
      ok "postgres redis qdrant healthy"
      return 0
    fi
    sleep 2
  done
  fail "stack not healthy in time (pg=$pg_ok redis=$redis_ok qdrant=$qdrant_ok)"
  exit 1
}

run_lint() {
  section "lint"
  if "$PYTHON" -m ruff check . && "$PYTHON" -m ruff format --check .; then
    ok "ruff"
  else
    fail "ruff"
    exit 1
  fi
}

run_unit() {
  section "unit (not integration and not live)"
  if "$PYTHON" -m pytest -q -m 'not integration and not live'; then
    ok "unit"
  else
    fail "unit"
    exit 1
  fi
}

run_integration() {
  section "integration"
  if "$PYTHON" -m pytest -q -m integration; then
    ok "integration"
  else
    fail "integration"
    exit 1
  fi
}

live_ready() {
  "$PYTHON" - <<'PY'
from scripts.bench_lib import live_ready, load_dotenv_files, ROOT
load_dotenv_files(ROOT / ".env", ROOT / ".env.llm")
ok, detail = live_ready()
raise SystemExit(0 if ok else 1)
PY
}

run_sim_users() {
  section "sim users (new keys, new agents, ACL, approval)"
  if [[ "$SKIP_SIM" == "1" ]]; then
    skip "sim users (SKIP_SIM=1)"
    return 0
  fi
  # Hash embed keeps the journey fast; live LLM quality is covered by bench.
  if EMBEDDING_BACKEND="${EMBEDDING_BACKEND:-hash}" "$PYTHON" -m scripts.sim_users; then
    ok "sim users"
  else
    fail "sim users"
    exit 1
  fi
}

run_live() {
  section "live + bench"
  if [[ "$QUICK" == "1" || "$SKIP_LIVE" == "1" ]]; then
    skip "live/bench (QUICK or SKIP_LIVE)"
    return 0
  fi
  if ! live_ready; then
    skip "live/bench (no OpenAI-compatible credentials loaded)"
    return 0
  fi
  export EMBEDDING_BACKEND="${EMBEDDING_BACKEND:-fastembed}"
  export QDRANT_MEMORY="${QDRANT_MEMORY:-1}"
  if "$PYTHON" -m pytest -q -m live; then
    ok "live pytest"
  else
    fail "live pytest"
    exit 1
  fi
  if "$PYTHON" -m scripts.bench; then
    ok "bench -> reports/bench.json"
  else
    fail "bench"
    exit 1
  fi
}

summary() {
  section "summary"
  printf 'passed=%s failed=%s skipped=%s\n' "$pass_n" "$fail_n" "$skip_n"
  if [[ -f reports/bench.json ]]; then
    printf 'bench_report=%s\n' "reports/bench.json"
  fi
  if (( fail_n > 0 )); then
    exit 1
  fi
}

main() {
  preflight
  clear_stack
  start_stack
  wait_healthy
  run_lint
  run_unit
  run_integration
  run_sim_users
  run_live
  summary
}

main "$@"
