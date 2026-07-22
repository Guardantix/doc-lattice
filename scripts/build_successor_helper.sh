#!/usr/bin/env bash
# Build the successor shell-parser helper with its manifest-derived identity.
set -euo pipefail

if [[ $# -ne 1 || -z "$1" || "$1" != /* ]]; then
    echo "usage: build_successor_helper.sh ABSOLUTE_OUTPUT_PATH" >&2
    exit 2
fi

repo_root="$(cd "$(dirname "$0")/.." && pwd -P)"
out="$1"
out_parent="${out%/*}"
out_name="${out##*/}"
if [[ -z "$out_name" || ! -d "$out_parent" ]]; then
    echo "output must name a non-directory path in an existing directory" >&2
    exit 2
fi
if [[ -L "$out" || (-e "$out" && ! -f "$out") ]]; then
    echo "existing output must be a regular non-symlink file" >&2
    exit 2
fi
out_parent="$(cd "$out_parent" && pwd -P)"
out="$out_parent/$out_name"
case "$out" in
    "$repo_root"|"$repo_root"/*)
        echo "output must be outside the repository" >&2
        exit 2
        ;;
esac

digest="$(/usr/bin/python3 -I "$repo_root/scripts/check_helper_digest.py")"
if [[ ! "$digest" =~ ^[0-9a-f]{64}$ ]]; then
    echo "helper digest script returned an invalid digest" >&2
    exit 2
fi

cd "$repo_root/helper/doc-lattice-shell-parser"
CGO_ENABLED=0 GO111MODULE=on GOENV=off GOFLAGS='' GOROOT=/usr/local/go GOTOOLCHAIN=local GOWORK=off \
    /usr/local/go/bin/go build -trimpath -ldflags "-X main.helperVersion=$digest" -o "$out" .
if [[ -L "$out" || ! -f "$out" || ! -x "$out" ]]; then
    echo "build did not produce a regular executable output" >&2
    exit 2
fi
