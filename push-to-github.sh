#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Options Ollie — Push to GitHub
# Run this once from your terminal to create the GitHub repo and push v1.
#
# Prerequisites:
#   - GitHub account
#   - GitHub CLI installed: https://cli.github.com
#     (Mac: brew install gh   |   Windows: winget install GitHub.cli)
#
# Usage:
#   chmod +x push-to-github.sh
#   ./push-to-github.sh
# ─────────────────────────────────────────────────────────────────────────────

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUNDLE="$SCRIPT_DIR/options-ollie-v1.bundle"
REPO_NAME="options-ollie"

echo "🦉 Options Ollie — GitHub Push Script"
echo ""

# 1. Log into GitHub CLI if needed
if ! gh auth status &>/dev/null; then
    echo "→ Logging into GitHub..."
    gh auth login
fi

echo "→ Creating GitHub repository: $REPO_NAME"
gh repo create "$REPO_NAME" \
    --description "Local web dashboard for options income trading — covered calls, CSPs, iron condors, position monitoring and risk analysis" \
    --public \
    --confirm 2>/dev/null || echo "(repo may already exist, continuing)"

REMOTE_URL=$(gh repo view "$REPO_NAME" --json url -q .url 2>/dev/null || echo "")

# 2. Restore the repo from bundle
TMPDIR_REPO=$(mktemp -d)
echo "→ Restoring from bundle..."
git clone "$BUNDLE" "$TMPDIR_REPO/options-ollie" --branch main

cd "$TMPDIR_REPO/options-ollie"

# 3. Set remote and push
git remote add origin "$(gh repo view "$REPO_NAME" --json sshUrl -q .sshUrl 2>/dev/null || echo "git@github.com:$(gh api user -q .login)/$REPO_NAME.git")"
echo "→ Pushing main branch..."
git push -u origin main
echo "→ Pushing v1.0.0 tag..."
git push origin v1.0.0

# 4. Done
echo ""
echo "✅ Done! Your repo is live at:"
gh repo view "$REPO_NAME" --json url -q .url 2>/dev/null || echo "  https://github.com/$(gh api user -q .login)/$REPO_NAME"

# Cleanup
cd /
rm -rf "$TMPDIR_REPO"
