#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Options Ollie — Push to GitHub
# Run this from your terminal to push the latest version.
#
# Prerequisites:
#   - GitHub CLI: https://cli.github.com  (Mac: brew install gh)
#   - Logged in: gh auth login
#
# Usage:
#   chmod +x push-to-github.sh
#   ./push-to-github.sh
# ─────────────────────────────────────────────────────────────────────────────

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="kashman3000/options-ollie"

echo "🦉 Options Ollie — GitHub Push Script"
echo ""

# Log into GitHub CLI if needed
if ! gh auth status &>/dev/null; then
    echo "→ Logging into GitHub..."
    gh auth login
fi

# Find the latest bundle
BUNDLE=$(ls -t "$SCRIPT_DIR"/options-ollie-v*.bundle 2>/dev/null | head -1)
if [ -z "$BUNDLE" ]; then
    echo "❌ No bundle file found in $SCRIPT_DIR"
    exit 1
fi
echo "→ Using bundle: $(basename $BUNDLE)"

# Clone from bundle into temp dir
TMPDIR_REPO=$(mktemp -d)
git clone "$BUNDLE" "$TMPDIR_REPO/options-ollie" --branch main
cd "$TMPDIR_REPO/options-ollie"

# Set the remote
git remote set-url origin "https://github.com/$REPO.git" 2>/dev/null || \
    git remote add origin "https://github.com/$REPO.git"

# Push main + all tags
echo "→ Pushing to https://github.com/$REPO ..."
git push origin main --force
git push origin --tags

echo ""
echo "✅ Done! https://github.com/$REPO"

# Cleanup
cd /
rm -rf "$TMPDIR_REPO"
