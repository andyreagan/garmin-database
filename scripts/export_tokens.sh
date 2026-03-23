#!/usr/bin/env bash
# Run this after `python garmin_db.py login` to push the OAuth tokens
# to GitHub Actions secrets so the scheduled workflow can authenticate.
#
# Usage: bash scripts/export_tokens.sh

set -euo pipefail

GARTH_DIR="${GARMIN_TOKEN_STORE:-.garth}"
REPO="andyreagan/garmin-database"

if [ ! -f "$GARTH_DIR/oauth1_token.json" ] || [ ! -f "$GARTH_DIR/oauth2_token.json" ]; then
  echo "ERROR: token files not found in $GARTH_DIR/"
  echo "Run:  python garmin_db.py login   first."
  exit 1
fi

echo "Uploading oauth1_token.json → GARMIN_TOKENS_B64 ..."
base64 < "$GARTH_DIR/oauth1_token.json" | \
  gh secret set GARMIN_TOKENS_B64 --repo "$REPO"

echo "Uploading oauth2_token.json → GARMIN_OAUTH2_B64 ..."
base64 < "$GARTH_DIR/oauth2_token.json" | \
  gh secret set GARMIN_OAUTH2_B64 --repo "$REPO"

echo "Done. Secrets updated on $REPO"
echo "The scheduled workflow will now use these tokens."
echo "Re-run this script if you ever need to re-authenticate (tokens expire ~90 days)."
