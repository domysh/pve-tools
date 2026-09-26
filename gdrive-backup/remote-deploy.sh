#!/usr/bin/env bash
# Copy this tool to a Proxmox VE node and run pve_gdrive_backup.py there.
#
# Usage: ./remote-deploy.sh root@<node> [setup|status|run|test-notification|uninstall [--purge]]
#        (arguments default to "setup")
#
# The setup forwards the Google sign-in redirect (port 53682) over SSH: open
# the link it prints in a browser on this machine and it completes by itself.
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 root@<node> [setup|status|run|test-notification|uninstall [--purge]]" >&2
    exit 2
fi

target="$1"
shift
remote_dir="/root/pve-tools/gdrive-backup"
tool_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
oauth_port=53682

# COPYFILE_DISABLE keeps macOS tar from adding AppleDouble (._*) files.
COPYFILE_DISABLE=1 tar -C "${tool_dir}" --exclude=__pycache__ -czf - . \
    | ssh "${target}" "mkdir -p '${remote_dir}' && tar -xzf - --no-same-owner -C '${remote_dir}'"

if [[ $# -eq 0 ]]; then
    set -- setup
fi
forward=()
if [[ "$1" == setup ]]; then
    forward=(-o ExitOnForwardFailure=no -L "${oauth_port}:127.0.0.1:${oauth_port}")
fi
# ${forward[@]+...}: an empty array is "unbound" for bash 3.2 (macOS) under set -u.
ssh -t ${forward[@]+"${forward[@]}"} "${target}" "python3 '${remote_dir}/pve_gdrive_backup.py' $(printf '%q ' "$@")"
