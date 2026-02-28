#!/usr/bin/env bash
set -euo pipefail

WITH_VOICE=1

for arg in "$@"; do
  case "$arg" in
    --no-voice)
      WITH_VOICE=0
      ;;
    --voice)
      WITH_VOICE=1
      ;;
    *)
      echo "Unknown option: $arg"
      echo "Usage: scripts/bootstrap.sh [--voice|--no-voice]"
      exit 2
      ;;
  esac
done

if ! command -v uv >/dev/null 2>&1; then
  echo "[bootstrap] Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

if [ "$WITH_VOICE" -eq 1 ] && [ "$(uname -s)" = "Linux" ] && command -v apt >/dev/null 2>&1; then
  echo "[bootstrap] Installing Ubuntu audio dependencies..."
  sudo apt update
  sudo apt install -y libportaudio2 portaudio19-dev pulseaudio-utils alsa-utils
fi

echo "[bootstrap] Installing emergent..."
uv tool install . --reinstall

if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  echo "[bootstrap] Created .env from .env.example"
fi

if [ "$WITH_VOICE" -eq 1 ]; then
  echo "[bootstrap] Running voice diagnostics..."
  uv run python scripts/voice_check.py || true
fi

echo "[bootstrap] Done"
