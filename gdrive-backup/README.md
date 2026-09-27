# gdrive-backup

Automatic upload of Proxmox VE backups to Google Drive, configured by an
interactive wizard that walks you through every step, Google sign-in included.

* **Event driven**: the upload starts as soon as a backup job to the chosen
  storage ends, scheduled or manual, without keeping the backup task running.
* **Same retention as the node** (mirror mode): what the backup job prunes
  locally is deleted from Drive too, to the Drive trash or permanently. A copy
  mode that never deletes is available as well.
* **Optional client-side encryption** with rclone crypt: Google only stores
  ciphertext, file names stay readable to pick the backup to restore.
* **Least privilege**: by default the Google token can only see the files this
  tool created (`drive.file` scope), not the rest of your Drive.
* **Notifications through Proxmox**: failed (optionally also successful)
  uploads go to the targets of Datacenter > Notifications, like backup jobs.
* **Safe by construction**: only finished vzdump files are uploaded, and a
  missing or empty storage never wipes the backups on Drive.

## How it works

```
 backup job (UI) --> storage "hdd-backup" (dir)/dump
        |
        | job-end: /etc/vzdump.conf  script: .../vzdump-hook.sh
        |          (only when the job wrote to the configured storage)
        v
 pve-gdrive-backup.service  (oneshot, idle I/O priority)
        |  rclone sync <storage>/dump  -->  gdrive:<folder>/dump
        |  (through the crypt remote when encryption is on)
        v
 Proxmox notification on failure / success
```

* The hook only flags a pending upload and starts the service with
  `--no-block`. If another backup job ends while an upload is running, the
  service uploads again right after.
* rclone uploads 4 files at a time in 64 MiB chunks, retries, stops at the
  750 GiB/day upload quota of Google Drive, and skips archives still being
  written (`*.dat`) and staging directories (`*.tmp`).
* In mirror mode, if the storage has no backups at all while Drive has some,
  the upload is refused and reported instead of deleting everything on Drive
  (typical cause: the disk behind the storage is not mounted).

## Setup

As root in the shell of the node that holds the backups:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/domysh/pve-tools/main/gdrive-backup/install.sh)"
```

It runs the setup wizard, which installs the tool on the node: afterwards the
`pve-gdrive-backup` command is available (see Operations). At the end of the
Google sign-in the browser is sent to `http://127.0.0.1:53682/`. From the web
shell that page fails to load: paste its address into the wizard. Over SSH,
connect with `ssh -L 53682:127.0.0.1:53682 root@<node>` and the sign-in
completes by itself.

The wizard asks, in order:

1. **Prerequisites**: installs `rclone` from the Debian repositories if it is
   missing.
2. **Backup storage**: one of the directory storages with backup content of
   this node, showing size and the backup jobs that write to it.
3. **Google account**: reuse an rclone Google Drive remote already configured
   in `/root/.config/rclone/rclone.conf`, or connect an account. For a new
   connection it guides you through creating your own OAuth client in the
   Google Cloud console (project, Drive API, consent screen, "Desktop app"
   client), with the links to each page, then prints the sign-in link.
4. **Folder on Drive and encryption**: the storage's `dump/` directory goes to
   `<folder>/dump`. The wizard shows what the folder already contains: files
   already there are not uploaded again.
5. **Upload behaviour**: mirror or copy, trash or permanent deletion,
   bandwidth limit, notifications.
6. **Review and install**: if `/etc/vzdump.conf` already has a hook script,
   the wizard shows it and lets you replace it or keep it chained after this
   tool's hook. Nothing is installed before this final confirmation.

Run the wizard again at any time to change the configuration: it starts from
the current values. The command above also updates the installed tool to the
latest version; `pve-gdrive-backup setup` reconfigures the installed one.

### Without questions

Every question of the wizard has an option of `setup` that answers it, and
`-y` asks nothing else: the rest comes from the current configuration or the
defaults. So `-- setup -y` alone updates the tool and keeps the configuration,
and a whole setup fits in one command:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/domysh/pve-tools/main/gdrive-backup/install.sh)" -- setup -y \
    --storage hdd-backup --remote gdrive --folder Backups/Proxmox/node1 \
    --no-encrypt --mode mirror --no-trash --bwlimit 30M
```

| Option | Answers |
| --- | --- |
| `--storage ID` | backup storage; required with `-y` if the node has several |
| `--remote NAME` | rclone Google Drive remote to use; with `--token`, the one to create or update (default `gdrive`) |
| `--client-id ID`, `--client-secret SECRET` | your OAuth client, for a new remote |
| `--scope drive.file\|drive` | access scope of a new remote (default `drive.file`) |
| `--token JSON` | a sign-in done on another computer, see below |
| `--folder PATH` | folder on Drive (default `Backups/Proxmox/<node>`) |
| `--encrypt`, `--no-encrypt` | encryption; required with `-y` on a first setup |
| `--mode mirror\|copy` | what happens to backups pruned locally (default `mirror`) |
| `--trash`, `--no-trash` | deleted backups go to the Drive trash, or are deleted permanently |
| `--bwlimit LIMIT` | rclone bandwidth limit, `''` for none |
| `--notify-failure`, `--notify-success` | notifications; `--no-notify-...` turns them off |
| `--existing-hook replace\|chain` | a hook script already in `/etc/vzdump.conf`; required with `-y` if there is one |
| `--upload-now`, `--no-upload-now` | upload the current backups right away (default yes) |

With `-y` new encryption passwords are printed without waiting for you: save
them from the output.

**Signing in to Google without a browser on the node.** The sign-in needs a
browser, but not on the node: on a computer with a browser and
[rclone](https://rclone.org/downloads/), sign in with your OAuth client

```bash
rclone authorize drive "$(printf '{"client_id":"<client id>","client_secret":"<client secret>","scope":"drive.file"}' | base64 | tr '+/' '-_' | tr -d '=\n')"
```

and pass the token it prints, the whole `{...}`, with
`--token '<token>' --client-id <client id> --client-secret <client secret>`.
The setup tries the token before saving the remote, so a wrong one never
replaces a working remote. `setup -y --client-id ... --client-secret ...`
without an account prints this `rclone authorize` command ready to run. Like
every option, the token stays in the shell history, and it grants access to
your Drive.

### The Google OAuth client

Google needs an OAuth client to let rclone use Drive. rclone's shared client is
heavily rate limited, so the wizard has you create your own (free). Two
settings matter:

* **Publish the app** (Google Auth Platform > Audience > "Publish app"). In the
  "Testing" state Google revokes the login after 7 days and the uploads start
  failing. Publishing needs no verification for your own account: Google just
  shows an "unverified app" warning at sign-in ("Advanced" > "Go to ...").
* **Application type "Desktop app"**, which allows the local redirect used by
  the sign-in.

If the token of an existing remote stops working, the wizard offers to sign in
again with the same OAuth client.

### Access scope

* `drive.file` (default for new connections): the token only sees what this
  tool created. Let the tool create the backup folder: a folder you create by
  hand in the Drive web UI is invisible to it, and you would end up with two
  folders with the same name.
* `drive`: full access, only needed to upload into a folder created by
  something else.

### Encryption

With encryption on, the wizard creates the rclone crypt remote
`pve-gdrive-backup-crypt` on top of `<drive remote>:<folder>` and shows its two
passwords once: **store them in a password manager**. They are also in
`rclone.conf` (obfuscated, not encrypted), but if the node is lost they are the
only way to decrypt the backups. Files keep their names with a `.bin` suffix.

## Operations

On the node:

```bash
pve-gdrive-backup status              # configuration, last upload, local vs Drive
pve-gdrive-backup run                 # upload now (in the background)
journalctl -fu pve-gdrive-backup      # follow the upload log
pve-gdrive-backup test-notification   # check that notifications reach you
```

Notifications carry the field `type=gdrive-backup`, so a notification matcher
can route them separately from the backup jobs (`type=vzdump`).

Configuration: `/etc/pve-gdrive-backup.conf` (written by the wizard).
Installed files: `/usr/local/lib/pve-gdrive-backup/`, the command
`/usr/local/sbin/pve-gdrive-backup`, the unit
`/etc/systemd/system/pve-gdrive-backup.service` and the notification templates
in `/etc/pve/notification-templates/default/` (shared by the cluster). The
outcome of the last upload is kept in `/var/lib/pve-gdrive-backup/`.

## Restore

Copy the archive back into a backup storage and restore it from the UI
(storage > Backups) or with `qmrestore`/`pct restore`:

```bash
rclone lsf gdrive:Backups/Proxmox/node1/dump                  # list the backups
rclone copy --progress \
    gdrive:Backups/Proxmox/node1/dump/vzdump-qemu-100-2026_09_26-21_00_04.vma.zst \
    /var/lib/vz/dump/
```

With encryption, read through the crypt remote instead
(`pve-gdrive-backup-crypt:dump/...`). On a machine without the tool, create the
Google Drive remote with `rclone config`, then a `crypt` remote with
`remote = <drive remote>:<folder>`, `filename_encryption = off`,
`directory_name_encryption = false` and the two saved passwords.

## Uninstall

```bash
pve-gdrive-backup uninstall            # hook, service and program
pve-gdrive-backup uninstall --purge    # also configuration and templates
```

Both ask for confirmation (`-y` skips it). The hook script that was configured before the setup is put back. The rclone
remotes (and the encryption passwords in them) and the backups on Google Drive
are always kept.

## Notes and limits

* **One storage per node.** Run the setup on every node whose backups should be
  uploaded, with a different Drive folder for each. For a storage shared by
  several nodes (NFS, CIFS...), set it up on one node only.
* **Directory storages only.** Proxmox Backup Server storages are not plain
  directories: replicate them with PBS itself (sync jobs, remotes).
* **Per-job hook scripts** (`script` set on a single backup job) replace the one
  in `/etc/vzdump.conf`: backups of such jobs do not trigger an upload. The
  wizard warns about them.
* **Google Drive quotas**: 750 GiB of uploads per day per account; backups in
  the Drive trash still count against your storage quota until the trash is
  emptied (30 days).
