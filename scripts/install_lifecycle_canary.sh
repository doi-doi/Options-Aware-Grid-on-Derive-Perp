#!/usr/bin/env bash
set -euo pipefail

# Install the reviewed Hummingbot-native canary only. This script never starts
# a bot, changes credentials, installs an evidence artifact, or arms mainnet.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
runtime_root="${HUMMINGBOT_API_DIR:-$repo_root/../hummingbot-api}"
destination="$runtime_root/bots/scripts/derive_options_grid_mainnet_lifecycle_canary.py"
config_destination="$runtime_root/bots/conf/scripts/derive_options_grid_stage_a_canary.yml"
stage_b_destination="$runtime_root/bots/scripts/derive_options_grid_mainnet_stage_b_canary.py"
stage_b_config_destination="$runtime_root/bots/conf/scripts/derive_options_grid_stage_b_canary.yml"
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

for target in "$destination" "$config_destination" "$stage_b_destination" "$stage_b_config_destination"; do
  if [[ -e "$target" ]] && ! grep -q "$marker" "$target" 2>/dev/null; then
    echo "refusing to overwrite unmarked path: $target" >&2
    exit 1
  fi
done

echo "canary: $destination"
echo "config: $config_destination"
echo "stage B canary: $stage_b_destination"
echo "stage B config: $stage_b_config_destination"
echo "mode: gated Hummingbot-native Stage A canary (dry-run by default)"
if (( dry_run )); then
  exit 0
fi

mkdir -p "$(dirname "$destination")"
mkdir -p "$(dirname "$config_destination")"
mkdir -p "$(dirname "$stage_b_destination")"
mkdir -p "$(dirname "$stage_b_config_destination")"
install -m 0644 "$repo_root/scripts/derive_options_grid_mainnet_lifecycle_canary.py" "$destination"
install -m 0644 "$repo_root/configs/derive_options_grid_stage_a_canary.yml" "$config_destination"
install -m 0644 "$repo_root/scripts/derive_options_grid_mainnet_stage_b_canary.py" "$stage_b_destination"
install -m 0644 "$repo_root/configs/derive_options_grid_stage_b_canary.yml" "$stage_b_config_destination"
echo "installed gated canary; no bot was started"
