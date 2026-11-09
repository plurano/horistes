# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
#
# Bounded, digest-verified release downloads for scripts/ci/install-*.sh
# (sourced, never executed; callers set strict mode).
# If a github.com release download fails, fetch_release_fallback fetches
# the asset via gh release download (which reads GH_TOKEN from the environment).
# Deadlines use bash builtins and sleep, not GNU timeout, which macOS lacks.
# shellcheck shell=bash

# download_bounds_valid <max-bytes> <timeout-seconds>
# refuses a bound that is not a plain positive decimal: bash arithmetic would
# evaluate any other text, command substitutions in array subscripts included.
download_bounds_valid() {
	if [[ ! "$1" =~ ^[1-9][0-9]{0,15}$ ]]; then
		printf 'invalid max bytes, not a positive whole number: %s\n' "$1" >&2
		return 1
	fi
	if [[ ! "$2" =~ ^[1-9][0-9]{0,5}$ ]]; then
		printf 'invalid timeout seconds, not a positive whole number: %s\n' "$2" >&2
		return 1
	fi
}

# run_with_deadline <seconds> <command> [arguments...]
# runs the command and sends it TERM once the seconds pass, then KILL if it is
# still running 5 seconds later; returns its status. The command thus ends at
# most 5 seconds after its deadline. The watcher sets its TERM trap before it
# starts sleep and the trap stops every job it started, so stopping the watcher
# at any point leaves no sleep behind.
run_with_deadline() {
	local seconds="$1" grace_seconds=5
	shift
	"$@" &
	local child=$!
	(
		trap 'kill $(jobs -p) 2>/dev/null; exit 0' TERM
		sleep "$seconds" &
		wait "$!"
		if kill -TERM "$child" 2>/dev/null; then
			sleep "$grace_seconds" &
			wait "$!"
			kill -KILL "$child" 2>/dev/null
		fi
	) </dev/null >/dev/null 2>&1 &
	local watcher=$! status=0
	wait "$child" || status=$?
	# kill fails once the watcher has fired the deadline and exited. Otherwise its
	# trap stops its sleep and it exits 0; any other status means TERM reached it
	# before it set the trap, when it had started no sleep. Neither changes the
	# call's status.
	if kill -TERM "$watcher" 2>/dev/null && ! wait "$watcher" 2>/dev/null; then
		: # stopped before its trap; no sleep to stop
	fi
	return "$status"
}

# fetch_release_fallback <url> <max-bytes> <timeout-seconds> <output>
# fetches a github.com release asset using gh release download when curl fails.
# gh writes to a pipe that head cuts one byte past max-bytes, so an oversized
# asset stops at that size; a failed or oversized fetch leaves no file.
fetch_release_fallback() {
	local url="$1" max_bytes="$2" timeout_seconds="$3" output="$4"
	local url_pattern='^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/releases/download/([A-Za-z0-9_.+-]+)/([A-Za-z0-9_.+-]+)$'
	if [[ ! "$url" =~ $url_pattern ]]; then
		return 1
	fi
	local repo="${BASH_REMATCH[1]}" tag="${BASH_REMATCH[2]}" asset="${BASH_REMATCH[3]}"
	download_bounds_valid "$max_bytes" "$timeout_seconds" || return 1
	if ! command -v gh >/dev/null 2>&1; then
		printf 'no gh on PATH for the release download fallback of %s\n' "$url" >&2
		return 1
	fi
	printf '::warning title=Release download fallback::%s failed; fetching it with gh release download\n' "$url"
	local -a statuses=(1 1)
	# The if keeps errexit off for the pipeline. Both branches read PIPESTATUS:
	# without pipefail the pipeline succeeds when only gh fails.
	# The host is named: without it gh takes GH_HOST or the checkout's remote host.
	if run_with_deadline "$timeout_seconds" gh release download "$tag" --repo "github.com/$repo" --pattern "$asset" --output - |
		head -c "$((max_bytes + 1))" >"$output"; then
		statuses=("${PIPESTATUS[@]}")
	else
		statuses=("${PIPESTATUS[@]}")
	fi
	local size=0
	if [[ -f "$output" ]]; then
		size="$(wc -c <"$output")"
		size="${size//[[:space:]]/}"
	fi
	if ((size > max_bytes)); then
		printf 'release download for %s exceeded max bytes %s; stopped and removed\n' "$url" "$max_bytes" >&2
		rm -f -- "$output"
		return 1
	fi
	if [[ "${statuses[0]}" != 0 || "${statuses[1]}" != 0 ]]; then
		printf 'gh release download for %s failed with status %s\n' "$url" "${statuses[0]}" >&2
		rm -f -- "$output"
		return 1
	fi
}

# fetch_verified <url> <expected-sha256> <max-bytes> <timeout-seconds> <output>
# downloads url over HTTPS only and fails unless the file's SHA-256 matches.
# timeout-seconds bounds curl with its retries, and again the fallback; each
# phase ends at most 5 seconds after its deadline (run_with_deadline).
fetch_verified() {
	local url="$1" expected="$2" max_bytes="$3" timeout_seconds="$4" output="$5" actual
	if [[ ! "$expected" =~ ^[0-9a-f]{64}$ ]]; then
		printf 'invalid pinned archive digest: %s\n' "$expected" >&2
		return 1
	fi
	download_bounds_valid "$max_bytes" "$timeout_seconds" || return 1
	run_with_deadline "$timeout_seconds" curl --fail --silent --show-error --location \
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
