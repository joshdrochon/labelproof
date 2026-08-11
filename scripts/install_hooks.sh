#!/bin/sh
# Wire this clone's git hooks (LP-016, LP-010).
#
# Run once after cloning:
#
#     ./scripts/install_hooks.sh
#
# `git clone` never brings hooks with it — .git/hooks is local to each clone — so a repo
# whose integrity depends on hooks has to make installing them one obvious command.
# Pointing core.hooksPath at the tracked .githooks/ directory means the hooks are
# versioned with the code and every clone gets the same ones.
#
# What you get:
#   pre-commit   refuse to commit a credential (SEC-6)
#   pre-push     refuse to push main; everything ships on a branch
#   post-commit  reproject TICKETS.md from the `Closes:` trailers in history
#
# Idempotent. Safe to re-run, and safe to run on a clone that already has it.

set -eu

root=$(git rev-parse --show-toplevel)
hooks="$root/.githooks"

if [ ! -d "$hooks" ]; then
    echo "install_hooks: $hooks does not exist — are you in the LabelProof repo?" >&2
    exit 1
fi

# The executable bit is tracked by git, but a clone through a zip or a filesystem that
# drops permissions loses it, and a non-executable hook fails silently: git skips it and
# says nothing. Set it explicitly rather than trusting the checkout.
chmod +x "$hooks"/* 2>/dev/null || true

git config core.hooksPath .githooks

echo "hooks installed: core.hooksPath -> .githooks"
for hook in "$hooks"/*; do
    [ -f "$hook" ] || continue
    if [ -x "$hook" ]; then
        echo "  ok       $(basename "$hook")"
    else
        echo "  NOT EXECUTABLE  $(basename "$hook") — it will be skipped silently" >&2
    fi
done
