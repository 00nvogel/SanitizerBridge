#!/usr/bin/env bash
set -euo pipefail
# $1 is an isolated tree, and cwd is this trusted script's own directory.
# This example shares all files except the following private/control paths.
# rm does not follow symlinks. Do not follow tree-provided symlinks in custom scripts.
rm -rf -- "$1/.github" "$1/private" "$1/customer-private"
