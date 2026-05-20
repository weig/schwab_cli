#!/bin/sh
# schwab_cli installer.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/weig/schwab_cli/main/install.sh | sh
#
# What it does:
#   1. Installs `uv` if missing (via the official astral.sh installer).
#   2. Runs `uv tool install` against the GitHub repo so `schwab` lands on
#      your PATH at ~/.local/bin/schwab.
#
# What it does NOT do:
#   - Configure your Schwab credentials. Run `schwab setup` after install
#     and paste your developer-portal client_id + client_secret.
#   - Install browser auto-login. That's webauto-cli, installed separately.
#   - Install the dataset cron. Run `schwab dataset cron install` once
#     you've subscribed to some symbols.

set -eu

REPO="${SCHWAB_CLI_REPO:-https://github.com/weig/schwab_cli}"
REF="${SCHWAB_CLI_REF:-main}"

say()  { printf '%s\n' "$*"; }
warn() { printf '!! %s\n' "$*" >&2; }
die()  { warn "$*"; exit 1; }

# --- uv -------------------------------------------------------------

if ! command -v uv >/dev/null 2>&1; then
    say "uv not found — installing via https://astral.sh/uv/install.sh ..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # The astral installer adds uv to ~/.local/bin but doesn't update
    # the current shell's PATH. Source the env file it drops if present.
    for env_file in "$HOME/.local/bin/env" "$HOME/.cargo/env"; do
        if [ -f "$env_file" ]; then
            # shellcheck disable=SC1090
            . "$env_file"
            break
        fi
    done
    command -v uv >/dev/null 2>&1 \
        || die "uv installed but not on PATH; open a new shell and re-run."
fi

say "Using uv: $(uv --version)"

# --- python 3.11+ check --------------------------------------------

# uv tool install will fetch a managed Python if the host doesn't have
# one >=3.11, so this is informational only.
if command -v python3 >/dev/null 2>&1; then
    PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "?")"
    say "System python3: $PY_VERSION (uv will fetch its own if <3.11)."
fi

# --- install schwab_cli --------------------------------------------

say "Installing schwab_cli from $REPO@$REF ..."
uv tool install --reinstall --from "git+$REPO@$REF" schwab_cli

# --- post-install hints --------------------------------------------

# `uv tool install` puts binaries in ~/.local/bin. Warn the user if it
# isn't on PATH so they don't get "command not found: schwab" next.
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *)
        warn "$HOME/.local/bin is not on your PATH."
        warn "Add this to your shell profile:"
        warn "    export PATH=\"\$HOME/.local/bin:\$PATH\""
        ;;
esac

cat <<'EOF'

Installed. Next steps:

    schwab setup         # paste your developer-portal client_id + client_secret
    schwab auth          # authenticate (browser opens for the first run)
    schwab doctor        # health check
    schwab quote NVDA    # smoke test

Need a Schwab developer account? https://developer.schwab.com
Project docs:                     https://github.com/weig/schwab_cli
EOF
