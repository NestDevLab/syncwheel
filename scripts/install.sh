#!/bin/sh
set -eu

usage() {
  cat <<'EOF'
Usage: scripts/install.sh [--with-uv] [--editable PATH]

Installs syncwheel as a uv tool.

Options:
  --with-uv        Install uv with the official astral.sh installer if uv is missing.
  --editable PATH  Install a local checkout in editable mode for development.
  -h, --help       Show this help.
EOF
}

with_uv=0
editable_path=

while [ "$#" -gt 0 ]; do
  case "$1" in
    --with-uv)
      with_uv=1
      shift
      ;;
    --editable)
      if [ "$#" -lt 2 ]; then
        echo "error: --editable requires a path" >&2
        exit 2
      fi
      editable_path=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! command -v uv >/dev/null 2>&1; then
  if [ "$with_uv" -ne 1 ]; then
    echo "error: uv is not installed. Install uv first or rerun with --with-uv." >&2
    exit 1
  fi
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    echo "error: --with-uv requires curl or wget" >&2
    exit 1
  fi
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv was installed but is not on PATH yet" >&2
  echo "Run: uv tool update-shell" >&2
  exit 1
fi

if [ -n "$editable_path" ]; then
  uv tool install --force --editable "$editable_path"
else
  uv tool install --force "git+https://github.com/NestDevLab/syncwheel"
fi

tool_bin_dir=${UV_TOOL_BIN_DIR:-"$HOME/.local/bin"}
case ":$PATH:" in
  *":$tool_bin_dir:"*) ;;
  *)
    echo "warning: uv tool bin directory is not on PATH: $tool_bin_dir" >&2
    echo "Run: uv tool update-shell" >&2
    ;;
esac

if command -v syncwheel >/dev/null 2>&1; then
  syncwheel --version
elif [ -x "$tool_bin_dir/syncwheel" ]; then
  "$tool_bin_dir/syncwheel" --version
else
  echo "warning: syncwheel was installed but the executable was not found on PATH" >&2
  echo "Run: uv tool update-shell" >&2
fi
