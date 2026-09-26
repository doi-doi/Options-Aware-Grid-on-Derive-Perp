#!/usr/bin/env bash
set -euo pipefail

# Install only the reviewed controller and pure support package. This script
# never edits an active config, starts a bot, arms mainnet, or calls an
# exchange. Existing files must carry our marker before they may be replaced.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
runtime_root="${HUMMINGBOT_API_DIR:-$repo_root/../hummingbot-api}"
controller_dest="$runtime_root/bots/controllers/market_making/derive_options_adaptive_grid.py"
support_root="$runtime_root/bots/controllers/market_making/derive_options_adaptive_grid_support"
support_dest="$support_root/derive_options_adaptive_grid"
marker="DERIVE_OPTIONS_ADAPTIVE_GRID_MANAGED"
dry_run=0

if [[ "${1:-}" == "--dry-run" ]]; then
  dry_run=1
elif [[ "${1:-}" == "--apply" || -z "${1:-}" ]]; then
  dry_run=0
else
  echo "usage: $0 [--dry-run|--apply]" >&2
  exit 2
fi

if [[ ! -d "$runtime_root/bots/controllers/market_making" ]]; then
  echo "Hummingbot controller directory not found: $runtime_root/bots/controllers/market_making" >&2
  exit 1
fi

check_replaceable() {
  local path="$1"
  if [[ -e "$path" ]] && ! grep -q "$marker" "$path" 2>/dev/null; then
    echo "refusing to overwrite unmarked path: $path" >&2
    exit 1
  fi
}

check_replaceable "$controller_dest"
check_replaceable "$support_dest/__init__.py"
if [[ -d "$support_dest" ]] && ! grep -R -q "$marker" "$support_dest" 2>/dev/null; then
  echo "refusing to overwrite unmarked support package: $support_dest" >&2
  exit 1
fi

echo "controller: $controller_dest"
echo "support package: $support_dest"
echo "config example is not installed or modified by this script"
if (( dry_run )); then
  exit 0
fi

mkdir -p "$support_root"
install -m 0644 "$repo_root/controllers/market_making/derive_options_adaptive_grid.py" "$controller_dest"
rm -rf "$support_dest"
mkdir -p "$support_dest"
cp -R "$repo_root/src/derive_options_adaptive_grid/." "$support_dest/"
echo "installed disarmed controller source; no bot was started"
