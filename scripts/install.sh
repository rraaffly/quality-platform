#!/usr/bin/env sh
set -eu
install_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if ! command -v uv >/dev/null 2>&1; then
  echo 'uv is required: https://docs.astral.sh/uv/getting-started/installation/' >&2
  exit 1
fi
cd "$install_root"
uv sync --locked --no-dev --python 3.12
entrypoint="$install_root/.venv/bin/quality-platform"
if [ ! -x "$entrypoint" ]; then
  echo "Missing launcher: $entrypoint" >&2
  exit 1
fi
user_bin="${QUALITY_PLATFORM_BIN_DIR:-${XDG_BIN_HOME:-$HOME/.local/bin}}"
mkdir -p "$user_bin"
ln -sfn "$entrypoint" "$user_bin/quality-platform"
echo "Installed $user_bin/quality-platform"
case ":$PATH:" in
  *":$user_bin:"*) echo 'Run: quality-platform setup' ;;
  *)
    if [ -z "${QUALITY_PLATFORM_BIN_DIR:-}" ]; then
      case "${SHELL:-}" in
        */zsh) shell_profile="$HOME/.zshrc" ;;
        */bash) shell_profile="$HOME/.bashrc" ;;
        *) shell_profile="$HOME/.profile" ;;
      esac
      if ! grep -Fq "$user_bin" "$shell_profile" 2>/dev/null; then
        printf '\nexport PATH="%s:$PATH"\n' "$user_bin" >> "$shell_profile"
      fi
      echo "Added $user_bin to PATH in $shell_profile. Open a new terminal, then run: quality-platform setup"
    else
      echo "Add $user_bin to PATH to use the command from another terminal"
    fi
    ;;
esac
