#!/usr/bin/env bash
# Copy this tool to a Proxmox VE node and run deploy.py there.
#
# Usage: ./remote-deploy.sh root@<node> [deploy.py arguments...]
#        (arguments default to "install")
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 root@<node> [install|status|uninstall [--purge]]" >&2
    exit 2
fi

target="$1"
shift
remote_dir="/root/pve-tools/isolated-net"
tool_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# COPYFILE_DISABLE keeps macOS tar from adding AppleDouble (._*) files.
COPYFILE_DISABLE=1 tar -C "${tool_dir}" --exclude=__pycache__ -czf - . \
    | ssh "${target}" "mkdir -p '${remote_dir}' && tar -xzf - --no-same-owner -C '${remote_dir}'"

if [[ $# -eq 0 ]]; then
    set -- install
fi
ssh -t "${target}" "python3 '${remote_dir}/deploy.py' $(printf '%q ' "$@")"
