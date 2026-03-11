#!/usr/bin/env bash
# fetch-hashes.sh — Query GitHub for latest custom node commit SHAs.
# Prints HCL-formatted output for copy-paste into docker-bake.hcl.
#
# Usage:
#   ./scripts/fetch-hashes.sh              # uses unauthenticated API
#   GITHUB_TOKEN=ghp_xxx ./scripts/fetch-hashes.sh  # authenticated (higher rate limit)

set -euo pipefail

# --- Config: repo -> variable name ---
declare -A REPOS=(
  ["ltdrdata/ComfyUI-Manager"]="MANAGER_SHA"
  ["kijai/ComfyUI-KJNodes"]="KJNODES_SHA"
  ["MoonGoblinDev/Civicomfy"]="CIVICOMFY_SHA"
  ["MadiatorLabs/ComfyUI-RunpodDirect"]="RUNPODDIRECT_SHA"
)

# --- Resolve paths ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BAKE_FILE="$SCRIPT_DIR/../docker-bake.hcl"

if [[ ! -f "$BAKE_FILE" ]]; then
  echo "ERROR: docker-bake.hcl not found at $BAKE_FILE" >&2
  exit 1
fi

# --- Auth header (optional) ---
AUTH_HEADER=()
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  AUTH_HEADER=(-H "Authorization: Bearer $GITHUB_TOKEN")
fi

# --- Read current hash from bake file (variable block spans multiple lines) ---
get_current_hash() {
  local var_name="$1"
  grep -A2 "variable \"${var_name}\"" "$BAKE_FILE" | sed -n 's/.*default *= *"\([^"]*\)".*/\1/p' | head -1 || echo "unknown"
}

# --- Fetch latest commit SHA (short, 12 chars) ---
fetch_latest_sha() {
  local repo="$1"
  local response
  response=$(curl -fsSL "${AUTH_HEADER[@]}" \
    -H "Accept: application/vnd.github.v3+json" \
    "https://api.github.com/repos/${repo}/commits?per_page=1" 2>/dev/null) || {
    echo "ERROR" ; return
  }
  echo "$response" | sed -n 's/.*"sha" *: *"\([a-f0-9]\{12\}\).*/\1/p' | head -1
}

# --- Main ---
echo "# Updated custom node hashes ($(date +%Y-%m-%d))"
echo "# Paste these into docker-bake.hcl to update"
echo ""

has_changes=false

for repo in "${!REPOS[@]}"; do
  var_name="${REPOS[$repo]}"
  current=$(get_current_hash "$var_name")
  latest=$(fetch_latest_sha "$repo")

  if [[ "$latest" == "ERROR" ]]; then
    echo "# ${var_name}: FAILED to fetch from ${repo}" >&2
    echo "variable \"${var_name}\" {"
    echo "  default = \"${current}\""
    echo "}"
    continue
  fi

  if [[ "$current" == "$latest" ]]; then
    echo "# ${var_name}: ${current} (unchanged)"
  else
    echo "# ${var_name}: ${current} -> ${latest} (CHANGED)"
    has_changes=true
  fi
  echo "variable \"${var_name}\" {"
  echo "  default = \"${latest}\""
  echo "}"
done

echo ""
if [[ "$has_changes" == true ]]; then
  echo "# ^ Copy the variable blocks above into docker-bake.hcl"
else
  echo "# All hashes are up to date."
fi
