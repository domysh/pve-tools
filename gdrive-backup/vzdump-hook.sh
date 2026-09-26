#!/usr/bin/env bash
# vzdump hook installed by pve-gdrive-backup ("script:" in /etc/vzdump.conf).
#
# vzdump runs it for every phase of every backup job. At job-end, if the job
# wrote to the storage configured in /etc/pve-gdrive-backup.conf, it flags a
# pending upload and starts the upload service without waiting for it, so the
# backup task ends right away. A hook configured before this one runs last when
# CHAIN_PREVIOUS_HOOK=yes, with the same arguments and environment.
set -u

# vzdump runs hooks with an empty environment.
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

readonly CONFIG=/etc/pve-gdrive-backup.conf
readonly PENDING_FLAG=/run/pve-gdrive-backup.pending
readonly SERVICE=pve-gdrive-backup.service

config_value() {
    [[ -r "${CONFIG}" ]] || return 0
    sed -n "s/^$1=//p" "${CONFIG}" | tail -n 1
}

if [[ "${1:-}" == job-end && -n "${STOREID:-}" && "${STOREID}" == "$(config_value STORAGE)" ]]; then
    touch "${PENDING_FLAG}"
    if systemctl start --no-block "${SERVICE}"; then
        echo "pve-gdrive-backup: upload to Google Drive started (journalctl -u pve-gdrive-backup)"
    else
        echo "pve-gdrive-backup: cannot start ${SERVICE}" >&2
    fi
fi

previous_hook="$(config_value PREVIOUS_HOOK)"
if [[ "$(config_value CHAIN_PREVIOUS_HOOK)" == yes && -n "${previous_hook}" ]]; then
    if [[ -x "${previous_hook}" ]]; then
        exec "${previous_hook}" "$@"
    fi
    echo "pve-gdrive-backup: previous hook ${previous_hook} is missing, skipped" >&2
fi
exit 0
