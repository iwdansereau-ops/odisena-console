#!/usr/bin/env bash
# Apply the Odisena Console branch-protection rulesets.
#
# GitHub's REST API accepts one payload per ruleset, so this script iterates the
# `rulesets[]` array in the manifest and POSTs (or PUTs, if a ruleset of the same
# name already exists) each one individually. `_`-prefixed metadata keys in the
# manifest are documentation only and are stripped before each API call.
#
# This script does NOT run automatically. Review the manifest, verify the
# RepositoryRole bypass actor_id (5=Admin by convention, but confirm for this
# repo), then run it manually with a token that has `administration:write`.
#
# Usage:
#   OWNER=iwdansereau-ops REPO=odisena-console ./apply-rulesets.sh
#
# Requires: gh (authenticated), jq.
set -euo pipefail

OWNER="${OWNER:-iwdansereau-ops}"
REPO="${REPO:-odisena-console}"
MANIFEST="$(dirname "$0")/odisena-branch-protection.rulesets.json"

command -v gh >/dev/null || { echo "error: gh CLI not found" >&2; exit 1; }
command -v jq >/dev/null || { echo "error: jq not found" >&2; exit 1; }
[ -f "$MANIFEST" ] || { echo "error: manifest not found: $MANIFEST" >&2; exit 1; }

echo "Reminder: verify bypass actor_id in the manifest is correct for ${OWNER}/${REPO}"
echo "         (RepositoryRole: 5=Admin, 4=Maintain, 2=Write, 1=Read by convention)."
echo

count="$(jq '.rulesets | length' "$MANIFEST")"
echo "Applying ${count} ruleset(s) to ${OWNER}/${REPO}..."

# Existing rulesets, keyed by name -> id (for update instead of duplicate create).
existing="$(gh api "repos/${OWNER}/${REPO}/rulesets" --jq 'map({(.name): .id}) | add' 2>/dev/null || echo '{}')"

for i in $(seq 0 $((count - 1))); do
  # Strip `_`-prefixed documentation keys from the ruleset payload.
  payload="$(jq -c ".rulesets[$i] | with_entries(select(.key | startswith(\"_\") | not))" "$MANIFEST")"
  name="$(echo "$payload" | jq -r '.name')"
  id="$(echo "$existing" | jq -r --arg n "$name" '.[$n] // empty')"

  if [ -n "$id" ]; then
    echo "  updating ruleset '$name' (id $id)"
    echo "$payload" | gh api -X PUT "repos/${OWNER}/${REPO}/rulesets/${id}" --input -
  else
    echo "  creating ruleset '$name'"
    echo "$payload" | gh api -X POST "repos/${OWNER}/${REPO}/rulesets" --input -
  fi
done

echo "Done."
