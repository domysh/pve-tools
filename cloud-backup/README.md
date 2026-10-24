# cloud-backup

Automatic upload of Proxmox VE backups to the cloud with
[rclone](https://rclone.org/), configured by an interactive wizard that walks
you through every step. Any provider rclone supports holds the backups: Google
Drive, Proton Drive, Dropbox, OneDrive, S3, Backblaze B2, a WebDAV box, another
SSH host...

* **Event driven**: the upload starts as soon as a backup job to the chosen
  storage ends, scheduled or manual, without keeping the backup task running.
* **Same retention as the node** (mirror mode): what the backup job prunes
  locally is deleted from the remote too. A copy mode that never deletes is
  available as well.
* **Optional client-side encryption** with rclone crypt: the provider only
  stores ciphertext, file names stay readable to pick the backup to restore.
* **Guided sign-in for Google Drive** (own OAuth client, `drive.file` scope, no
  browser needed on the node); every other provider is set up by rclone's own
  `rclone config`, straight from the wizard.
* **Notifications through Proxmox**: failed (optionally also successful)
  uploads go to the targets of Datacenter > Notifications, like backup jobs.
* **Safe by construction**: only finished vzdump files are uploaded, and a
  missing or empty storage never wipes the backups on the remote.

## How it works

```
 backup job (UI) --> storage "hdd-backup" (dir)/dump
        |
        | job-end: /etc/vzdump.conf  script: .../vzdump-hook.sh
        |          (only when the job wrote to the configured storage)
        v
 pve-cloud-backup.service  (oneshot, idle I/O priority)
        |  rclone sync <storage>/dump  -->  <remote>:<folder>/dump
        |  (through the crypt remote when encryption is on)
        v
 Proxmox notification on failure / success
```

* The hook only flags a pending upload and starts the service with
  `--no-block`. If another backup job ends while an upload is running, the
  service uploads again right after.
* rclone uploads 4 files at a time, retries, and skips archives still being
  written (`*.dat`) and staging directories (`*.tmp`). Backend-specific flags
  are added for the remote's type (for Google Drive: 64 MiB chunks and a stop
  at the 750 GiB/day upload quota instead of retrying all day).
* In mirror mode, if the storage has no backups at all while the remote has
  some, the upload is refused and reported instead of deleting everything
  (typical cause: the disk behind the storage is not mounted).

## Setup

As root in the shell of the node that holds the backups:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/domysh/pve-tools/main/cloud-backup/install.sh)"
```

It runs the setup wizard, which installs the tool on the node: afterwards the
`pve-cloud-backup` command is available (see Operations).

The wizard asks, in order:

1. **Prerequisites**: installs `rclone` from the Debian repositories if it is
   missing.
2. **Backup storage**: one of the directory storages with backup content of
   this node, showing size and the backup jobs that write to it.
3. **Remote**: use an rclone remote already configured in
   `/root/.config/rclone/rclone.conf`, connect a Google Drive account with the
   guided sign-in, or set up any other provider with `rclone config` (see
   below). The chosen remote is tested before the wizard goes on.
4. **Folder and encryption**: the storage's `dump/` directory goes to
   `<folder>/dump` on the remote. The wizard shows what the folder already
   contains: files already there are not uploaded again.
5. **Upload behaviour**: mirror or copy, trash or permanent deletion (only
   where the provider has a trash rclone can choose, i.e. Google Drive),
   bandwidth limit, notifications.
6. **Review and install**: if `/etc/vzdump.conf` already has a hook script,
   the wizard shows it and lets you replace it or keep it chained after this
   tool's hook. Nothing is installed before this final confirmation.

Run the wizard again at any time to change the configuration: it starts from
the current values. The command above also updates the installed tool to the
latest version; `pve-cloud-backup setup` reconfigures the installed one.

### Another provider (Proton Drive, S3, Dropbox...)

The third option of step 3 opens `rclone config`, which knows every backend
rclone supports: `n` for a new remote, a name, then the type (for example
`protondrive`) and its questions. `q` comes back to the wizard, which picks up
the remote just created and tests it. For Proton Drive rclone asks for the
account, the password, a 2FA code and the mailbox password if you set one; for
providers that need a browser it prints the `rclone authorize` command to run
on a computer that has one.

Remotes created beforehand with `rclone config` (or copied from another
machine) show up in step 3 as well, which is also how a setup with `-y` gets
its remote. The name `pve-cloud-backup-crypt` is reserved for the encryption
layer.

### Without questions

Every question of the wizard has an option of `setup` that answers it, and
`-y` asks nothing else: the rest comes from the current configuration or the
defaults. So `-- setup -y` alone updates the tool and keeps the configuration,
and a whole setup fits in one command:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/domysh/pve-tools/main/cloud-backup/install.sh)" -- setup -y \
    --storage hdd-backup --remote proton --folder Backups/Proxmox/node1 \
    --encrypt --mode mirror --bwlimit 30M
```

| Option | Answers |
| --- | --- |
| `--storage ID` | backup storage; required with `-y` if the node has several |
| `--remote NAME` | rclone remote that holds the backups; with `--token`, the Google Drive remote to create or update (default `gdrive`) |
| `--folder PATH` | folder on the remote (default `Backups/Proxmox/<node>`) |
| `--encrypt`, `--no-encrypt` | encryption; required with `-y` on a first setup |
| `--mode mirror\|copy` | what happens to backups pruned locally (default `mirror`) |
| `--trash`, `--no-trash` | deleted backups go to the provider's trash, or are deleted permanently (Google Drive only) |
| `--bwlimit LIMIT` | rclone bandwidth limit, `''` for none |
| `--notify-failure`, `--notify-success` | notifications; `--no-notify-...` turns them off |
| `--existing-hook replace\|chain` | a hook script already in `/etc/vzdump.conf`; required with `-y` if there is one |
| `--upload-now`, `--no-upload-now` | upload the current backups right away (default yes) |
| `--client-id ID`, `--client-secret SECRET`, `--scope`, `--token JSON` | Google Drive only, see below |

With `-y` the remote must already exist in `rclone.conf` (`--remote`), or be a
Google Drive account signed in elsewhere (`--token`). New encryption passwords
are printed without waiting for you: save them from the output.

## Google Drive

Step 3 can connect a Google account without any browser on the node: it guides
you through creating your own OAuth client (rclone's shared one is heavily rate
limited), prints the sign-in link and takes the authorization back.

At the end of the sign-in the browser is sent to `http://127.0.0.1:53682/`.
From the web shell that page fails to load: paste its address into the wizard.
Over SSH, connect with `ssh -L 53682:127.0.0.1:53682 root@<node>` and the
sign-in completes by itself.

Two settings of the OAuth client matter:

* **Publish the app** (Google Auth Platform > Audience > "Publish app"). In the
  "Testing" state Google revokes the login after 7 days and the uploads start
  failing. Publishing needs no verification for your own account: Google just
  shows an "unverified app" warning at sign-in ("Advanced" > "Go to ...").
* **Application type "Desktop app"**, which allows the local redirect used by
  the sign-in.

If the token of an existing Drive remote stops working, the wizard offers to
sign in again with the same OAuth client.

**Access scope** (`--scope`):

* `drive.file` (default for new connections): the token only sees what this
  tool created. Let the tool create the backup folder: a folder you create by
  hand in the Drive web UI is invisible to it, and you would end up with two
  folders with the same name.
* `drive`: full access, only needed to upload into a folder created by
  something else.

**Signing in without a browser on the node.** On a computer with a browser and
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

## Encryption

With encryption on, the wizard creates the rclone crypt remote
`pve-cloud-backup-crypt` on top of `<remote>:<folder>` and shows its two
passwords once: **store them in a password manager**. They are also in
`rclone.conf` (obfuscated, not encrypted), but if the node is lost they are the
only way to decrypt the backups. Files keep their names with a `.bin` suffix.

## Operations

On the node:

```bash
pve-cloud-backup status              # configuration, last upload, local vs remote
pve-cloud-backup run                 # upload now (in the background)
journalctl -fu pve-cloud-backup      # follow the upload log
pve-cloud-backup test-notification   # check that notifications reach you
```

Notifications carry the field `type=cloud-backup`, so a notification matcher
can route them separately from the backup jobs (`type=vzdump`).

Configuration: `/etc/pve-cloud-backup.conf` (written by the wizard).
Installed files: `/usr/local/lib/pve-cloud-backup/`, the command
`/usr/local/sbin/pve-cloud-backup`, the unit
`/etc/systemd/system/pve-cloud-backup.service` and the notification templates
in `/etc/pve/notification-templates/default/` (shared by the cluster). The
outcome of the last upload is kept in `/var/lib/pve-cloud-backup/`.

## Restore

Copy the archive back into a backup storage and restore it from the UI
(storage > Backups) or with `qmrestore`/`pct restore`:

```bash
rclone lsf proton:Backups/Proxmox/node1/dump                  # list the backups
rclone copy --progress \
    proton:Backups/Proxmox/node1/dump/vzdump-qemu-100-2026_09_26-21_00_04.vma.zst \
    /var/lib/vz/dump/
```

With encryption, read through the crypt remote instead
(`pve-cloud-backup-crypt:dump/...`). On a machine without the tool, create the
same remote with `rclone config`, then a `crypt` remote with
`remote = <remote>:<folder>`, `filename_encryption = off`,
`directory_name_encryption = false` and the two saved passwords.

## Uninstall

```bash
pve-cloud-backup uninstall            # hook, service and program
pve-cloud-backup uninstall --purge    # also configuration and templates
```

Both ask for confirmation (`-y` skips it). The hook script that was configured
before the setup is put back. The rclone remotes (and the encryption passwords
in them) and the backups on the remote are always kept.

## Coming from pve-gdrive-backup

This tool was Google Drive only and called `pve-gdrive-backup`. The setup
imports the old configuration (its storage, remote, folder and options are the
defaults of the wizard), reuses the encryption passwords of
`pve-gdrive-backup-crypt`, and removes the old hook, service, program, state
and configuration at the end. Nothing is re-uploaded. Two things to check
afterwards:

* a notification matcher filtering on `type=gdrive-backup` must now match
  `type=cloud-backup`;
* anything of yours calling `pve-gdrive-backup` (scripts, cron) must call
  `pve-cloud-backup`.

## Notes and limits

* **One storage per node.** Run the setup on every node whose backups should be
  uploaded, with a different folder for each. For a storage shared by several
  nodes (NFS, CIFS...), set it up on one node only.
* **Directory storages only.** Proxmox Backup Server storages are not plain
  directories: replicate them with PBS itself (sync jobs, remotes).
* **Per-job hook scripts** (`script` set on a single backup job) replace the one
  in `/etc/vzdump.conf`: backups of such jobs do not trigger an upload. The
  wizard warns about them.
* **The provider's own limits apply.** Google Drive: 750 GiB of uploads per day
  per account, and backups in the trash count against the storage quota until
  the trash is emptied (30 days). Proton Drive: uploads are encrypted client
  side by rclone, so they are slower and CPU-bound. Check rclone's page for the
  backend ([rclone.org](https://rclone.org/)) for the rest.
* **The rclone configuration must not be encrypted** (no `rclone config`
  password): the service runs unattended.

## Self-check

```bash
python3 cloud-backup/test_pve_cloud_backup.py
```

Checks the settings file (the old `DRIVE_REMOTE` key included), the
destination paths and the rclone command line per backend. Needs neither
Proxmox nor rclone.
