#!/bin/bash
# Install beside a symlink named real-codegraph. Record only the command category.
set -eu
bin_dir=$(cd -- "$(dirname -- "$0")" && pwd)
category=help
for arg in "$@"; do
    case "$arg" in
        --color|--no-color) continue ;;
        serve) exec "$bin_dir/real-codegraph" "$@" ;;  # MCP server startup
        -V|-v|--version) category=version ;;
        -h|--help) category=help ;;
        init|uninit|index|sync|status|query|explore|context|node|files|daemon|daemons|unlock|callers|callees|impact|affected|install|uninstall|telemetry|upgrade|version|help) category=$arg ;;
        *) category=other ;;
    esac
    break
done
printf '%s\n' "$category" >> "$bin_dir/cli-commands.txt"
exec "$bin_dir/real-codegraph" "$@"
