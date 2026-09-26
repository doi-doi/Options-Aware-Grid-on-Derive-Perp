#!/usr/bin/env bash
set -euo pipefail

# Install only the three reviewed read-only routines.  This script never
# starts a routine, touches a bot, changes credentials, or edits Hummingbot.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
condor_root="${CONDOR_DIR:-$repo_root/../condor}"
routine_dest="$condor_root/routines"
marker="DERIVE_OPTIONS_ADAPTIVE_GRID_CONDOR_MANAGED"
dry_run=0

case "${1:-}" in
  --dry-run) dry_run=1 ;;
  --apply) dry_run=0 ;;
  *)
    echo "usage: $0 [--dry-run|--apply]" >&2
    exit 2
    ;;
esac

if [[ ! -d "$routine_dest" ]]; then
  echo "Condor routine directory not found: $routine_dest" >&2
  exit 1
fi

legacy_health_sha="b9c6b4885093b85721fa5ee8a9624980c042f61b4254bc16721c41c57502fe65"
legacy_evidence_sha="b33d5cb08e894d231be2171bff6f076d32941e05568b339c1dfa7b8049c78163"

files=(
  derive_options_grid_health.py
  derive_options_grid_evidence.py
  derive_options_grid_shadow_capture.py
)

sha256_file() {
  shasum -a 256 "$1" | awk '{print $1}'
}

for name in "${files[@]}"; do
  source_path="$repo_root/condor/$name"
  target_path="$routine_dest/$name"
  if [[ ! -f "$source_path" ]]; then
    echo "source routine missing: $source_path" >&2
    exit 1
  fi
  if [[ -e "$target_path" ]]; then
    if cmp -s "$source_path" "$target_path"; then
      echo "up-to-date: $target_path"
      continue
    fi
    target_sha="$(sha256_file "$target_path")"
    allowed_legacy=0
    if [[ "$name" == "derive_options_grid_health.py" && "$target_sha" == "$legacy_health_sha" ]]; then
      allowed_legacy=1
    elif [[ "$name" == "derive_options_grid_evidence.py" && "$target_sha" == "$legacy_evidence_sha" ]]; then
      allowed_legacy=1
    fi
    if ! grep -q "$marker" "$target_path" 2>/dev/null && (( ! allowed_legacy )); then
      echo "refusing to overwrite unmarked routine: $target_path" >&2
      exit 1
    fi
  fi
  echo "routine: $target_path"
done

if (( dry_run )); then
  echo "dry-run only; no Condor routine was changed or started"
  exit 0
fi

for name in "${files[@]}"; do
  install -m 0644 "$repo_root/condor/$name" "$routine_dest/$name"
done
echo "installed reviewed read-only Condor routines; none was started"
