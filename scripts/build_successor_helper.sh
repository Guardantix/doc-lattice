#!/usr/bin/env bash
# Build the successor shell-parser helper as a static binary to a caller-provided path.
set -euo pipefail
out="${1:?usage: build_successor_helper.sh OUTPUT_PATH}"
cd "$(dirname "$0")/../helper/doc-lattice-shell-parser"
CGO_ENABLED=0 /usr/local/go/bin/go build -trimpath -o "$out" .
