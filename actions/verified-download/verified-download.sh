# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
#
# Bounded, digest-verified release downloads for scripts/ci/install-*.sh
# (sourced, never executed; callers set strict mode).
# shellcheck shell=bash

# fetch_verified <url> <expected-sha256> <max-bytes> <timeout-seconds> <output>
# downloads url over HTTPS only and fails unless the file's SHA-256 matches.
fetch_verified() {
	local url="$1" expected="$2" max_bytes="$3" timeout_seconds="$4" output="$5" actual
	if [[ ! "$expected" =~ ^[0-9a-f]{64}$ ]]; then
		printf 'invalid pinned archive digest: %s\n' "$expected" >&2
		return 1
	fi
	curl --fail --silent --show-error --location \
		--proto '=https' --proto-redir '=https' \
		--retry 3 --connect-timeout 20 \
		--max-time "$timeout_seconds" \
		--max-filesize "$max_bytes" \
		--output "$output" \
		"$url" || return
	actual="$(sha256sum "$output" | awk '{ print $1 }')" || return
	if [[ "$actual" != "$expected" ]]; then
		printf 'archive digest mismatch for %s: expected %s, got %s\n' "$url" "$expected" "$actual" >&2
		return 1
	fi
}
