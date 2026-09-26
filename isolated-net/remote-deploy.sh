#!/usr/bin/env bash
# Update the pve-tools clone on a Proxmox VE node and run deploy.py there.
#
# Usage: ./remote-deploy.sh root@<node> [deploy.py arguments...]
#        (arguments default to "install")
#
# The node keeps a clone of the whole repository in /root/pve-tools, updated
# from GitHub on every run: push your changes first.
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 root@<node> [install|status|uninstall [--purge]]" >&2
    exit 2
fi

target="$1"
shift
tool_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
node_repo="/root/pve-tools"
# The node clones anonymously over HTTPS: a GitHub SSH origin becomes its HTTPS URL.
repo_url="$(git -C "${tool_dir}" remote get-url origin | sed -E 's#^(ssh://)?git@github\.com[:/]#https://github.com/#')"

if [[ -n "$(git -C "${tool_dir}" status --porcelain)" ]] \
    || [[ "$(git -C "${tool_dir}" rev-parse HEAD)" != "$(git -C "${tool_dir}" rev-parse '@{upstream}' 2>/dev/null)" ]]; then
    echo "WARNING: local changes not pushed to GitHub: the node runs the pushed version" >&2
fi
ssh "${target}" "set -e
if [ -d '${node_repo}/.git' ]; then git -C '${node_repo}' pull --ff-only --quiet
else git clone --quiet '${repo_url}' '${node_repo}'; fi
git -C '${node_repo}' log -1 --format='${node_repo} on ${target}: %h %s'"

if [[ $# -eq 0 ]]; then
    set -- install
fi
ssh -t "${target}" "python3 '${node_repo}/isolated-net/deploy.py' $(printf '%q ' "$@")"
