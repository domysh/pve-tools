#!/usr/bin/env bash
# One-command launcher of isolated-net: run it as root in the shell of any
# node of the Proxmox VE cluster.
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/domysh/pve-tools/main/isolated-net/install.sh)"
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/domysh/pve-tools/main/isolated-net/install.sh)" -- <command> [options]
#
# It downloads the tool from GitHub into a temporary directory and runs
# deploy.py with the given arguments ("install" when there are none, "--help"
# lists the commands). Run from a clone of the repository, it uses that copy.
#
# PVE_TOOLS_REPO (owner/name) and PVE_TOOLS_REF (branch, tag or commit) choose
# what to download, e.g. to try a branch before merging it.
set -euo pipefail

tool=isolated-net
entry=deploy.py
if [[ $# -eq 0 ]]; then
    set -- install
fi

# Empty (bash -c) or a pipe (bash <(curl ...)) unless run from a file.
source_file="${BASH_SOURCE[0]:-}"
if [[ -f "${source_file}" && -f "$(dirname "${source_file}")/${entry}" ]]; then
    exec python3 "$(dirname "${source_file}")/${entry}" "$@"
fi

repo="${PVE_TOOLS_REPO:-domysh/pve-tools}"
ref="${PVE_TOOLS_REF:-main}"
workdir="$(mktemp -d)"
trap 'rm -rf "${workdir}"' EXIT
echo "==> downloading ${tool} from github.com/${repo} (${ref})"
curl -fsSL "https://codeload.github.com/${repo}/tar.gz/${ref}" -o "${workdir}/source.tar.gz"
tar -xzf "${workdir}/source.tar.gz" -C "${workdir}" --strip-components=1
python3 "${workdir}/${tool}/${entry}" "$@"
