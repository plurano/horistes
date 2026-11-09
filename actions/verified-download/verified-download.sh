# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
#
# Bounded, digest-verified release downloads for scripts/ci/install-*.sh
# (sourced, never executed; callers set strict mode).
# If a github.com release download fails, fetch_release_fallback fetches
# the asset via gh release download (which reads GH_TOKEN from the environment).
# shellcheck shell=bash

# fetch_release_fallback <url> <max-bytes> <timeout-seconds> <output>
# fetches a github.com release asset using gh release download when curl fails.
fetch_release_fallback() {
	local url="$1" max_bytes="$2" timeout_seconds="$3" output="$4"
	local url_pattern='^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/releases/download/([A-Za-z0-9_.+-]+)/([A-Za-z0-9_.+-]+)$'
	if [[ ! "$url" =~ $url_pattern ]]; then
		return 1
	fi
	local repo="${BASH_REMATCH[1]}" tag="${BASH_REMATCH[2]}" asset="${BASH_REMATCH[3]}"
	if ! command -v gh >/dev/null 2>&1; then
		printf 'no gh on PATH for the release download fallback of %s\n' "$url" >&2
		return 1
	fi
	printf '::warning title=Release download fallback::%s failed; fetching it with gh release download\n' "$url"
	timeout "$timeout_seconds" gh release download "$tag" --repo "$repo" --pattern "$asset" --output "$output" --clobber || return
	local size
	size="$(wc -c <"$output")"
	if ((size > max_bytes)); then
		printf 'release download for %s exceeded max bytes: size %s, max %s\n' "$url" "$size" "$max_bytes" >&2
		return 1
	fi
}

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
		"$url" || fetch_release_fallback "$url" "$max_bytes" "$timeout_seconds" "$output" || return
	actual="$(sha256sum "$output" | awk '{ print $1 }')" || return
	if [[ "$actual" != "$expected" ]]; then
		printf 'archive digest mismatch for %s: expected %s, got %s\n' "$url" "$expected" "$actual" >&2
		return 1
	fi
}
