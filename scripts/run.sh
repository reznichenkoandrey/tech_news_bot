#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "ERROR: .env file missing. Copy .env.example → .env and fill in credentials." >&2
  exit 1
fi

# Export all variables from .env into current shell environment
set -a
# shellcheck source=/dev/null
source .env
set +a

echo "Running tech-digest for chat $TELEGRAM_CHAT_ID..."

# Note: Claude Code context window acts as the LLM engine — no external API needed.
# This script only validates env and prints instructions for manual invocation.
echo "Now open Claude Code in this directory and run: /tech-digest"
