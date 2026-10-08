#!/usr/bin/env bash
# 启动 vendored daily_stock_analysis，只提供 API，不托管它自己的前端。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DSA="$ROOT/vendor/daily_stock_analysis"

read_dotenv_value() {
  local key="$1"
  if [[ ! -f "$ROOT/.env" ]]; then
    return 0
  fi
  awk -v wanted="$key" '
    $0 ~ "^[[:space:]]*" wanted "[[:space:]]*=" {
      sub(/^[^=]*=/, "")
      sub(/[[:space:]]+#.*$/, "")
      gsub(/^[[:space:]]+|[[:space:]]+$/, "")
      if (($0 ~ /^".*"$/) || ($0 ~ /^\047.*\047$/)) {
        $0 = substr($0, 2, length($0) - 2)
      }
      print
      exit
    }
  ' "$ROOT/.env"
}

if [[ ! -f "$DSA/main.py" ]]; then
  echo "找不到 $DSA/main.py" >&2
  exit 1
fi

if [[ -z "${DSA_PORT:-}" ]]; then
  DSA_PORT="$(read_dotenv_value DSA_PORT)"
fi
DSA_PORT="${DSA_PORT:-8000}"
DSA_HOST="${DSA_HOST:-127.0.0.1}"

export ENV_FILE="${ENV_FILE:-$ROOT/.env}"
export TZ="${TZ:-Asia/Shanghai}"
mkdir -p "$ROOT/data/dsa"
if [[ -z "${DATABASE_PATH:-}" ]]; then
  from_file="$(read_dotenv_value DATABASE_PATH)"
  export DATABASE_PATH="${from_file:-$ROOT/data/dsa/stock_analysis.db}"
fi

# 已有 TSP 模型配置、又没单独填 DSA 的 OpenAI 兼容密钥时，直接借用。
if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  borrowed="$(read_dotenv_value AI_API_KEY)"
  if [[ -n "$borrowed" ]]; then
    export OPENAI_API_KEY="$borrowed"
    if [[ -z "${OPENAI_BASE_URL:-}" ]]; then
      export OPENAI_BASE_URL="$(read_dotenv_value AI_BASE_URL)"
    fi
    if [[ -z "${OPENAI_MODEL:-}" ]]; then
      export OPENAI_MODEL="$(read_dotenv_value AI_MODEL)"
    fi
  fi
fi

if [[ ! -x "$DSA/.venv/bin/python" ]]; then
  if ! command -v uv >/dev/null 2>&1; then
    echo "需要 uv 来创建 DSA 虚拟环境: https://docs.astral.sh/uv/" >&2
    exit 1
  fi
  uv venv --python 3.11 "$DSA/.venv"
  uv pip install --python "$DSA/.venv/bin/python" -r "$DSA/requirements.txt"
fi

cd "$DSA"
exec "$DSA/.venv/bin/python" main.py --serve-only --host "$DSA_HOST" --port "$DSA_PORT"
