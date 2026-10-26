#!/bin/sh
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
#
# commit-msg hook: Conventional Commits subject, DCO Signed-off-by trailer, and no trailer
# or footer that names a model or coding tool. Cases: commit-msg_test.sh.
# Usage: sh .config/hooks/commit-msg.sh <path-to-COMMIT_EDITMSG>
set -eu

msgfile="$1"
subject="$(sed -e '/^#/d' "$msgfile" | sed -n '1p')"

if ! printf '%s' "$subject" | grep -Eq '^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([a-z0-9/_-]+\))?!?: .{1,72}$'; then
  echo "commit-msg: subject must be a Conventional Commit (type(scope)?: summary <= 72 chars)" >&2
  echo "  got: $subject" >&2
  exit 1
fi

if ! sed -e '/^#/d' "$msgfile" | grep -Eq '^Signed-off-by: .+ <.+@.+>$'; then
  echo "commit-msg: missing DCO trailer; commit with 'git commit -s'" >&2
  exit 1
fi

# Nothing in the history names the model or coding tool that produced a change. Refused: a
# co-author, assisted-by or generated-by trailer and a "generated with" footer that name one.
# A person stays a valid co-author, and a product named in prose passes. "claude" alone
# counts only in a footer, since it is also a first name; the vendor address still gives a
# trailer away.
tools='claude (code|opus|sonnet|haiku|fable)|anthropic|copilot|codex|openai|chatgpt|gemini|cursor|aider|devin[ -]ai|windsurf|codeium|tabnine|amazon q|antigravity|google-labs-jules'
named="(.*[^[:alnum:]])?($tools)([^[:alnum:]]|\$)"
trailer="^[[:space:]]*(co-authored-by|assisted-by|generated-by|generated-with)[[:space:]]*:$named"
footer="generated (with|by) (.*[^[:alnum:]])?(claude|$tools)([^[:alnum:]]|\$)"
status=0
found="$(sed -e '/^#/d' "$msgfile" | grep -Ein -e "$trailer" -e "$footer")" || status=$?
case "$status" in
  0)
    echo "commit-msg: a trailer or footer names a model or coding tool; remove the line" >&2
    printf '%s\n' "$found" | sed -e 's/^/  line /' >&2
    exit 1
    ;;
  1) ;;
  *)
    echo "commit-msg: the attribution check itself failed (grep exit $status)" >&2
    exit 2
    ;;
esac
