# pve-tools

A collection of tools for Proxmox VE. Each tool lives in its own directory,
is self-contained (its own scripts, configuration and README) and is installed
with one command in the shell of a node, in the style of the
[Proxmox VE Helper-Scripts](https://community-scripts.org/).

> **Personal tools.** I wrote these for my own Proxmox VE setup and share them
> as they are, in case they help someone else. They follow my needs, may change
> or break without notice and come with no support or warranty: read the code
> and try them on a test node before trusting them with your data.

## Tools

Run the commands as root in the shell of a Proxmox VE node (web UI > node >
Shell, or SSH). They ask before changing anything.

### isolated-net

Isolated, internet-only, dual-stack guest network built on SDN, with an agent
that keeps NAT and isolation in sync with the node's routing table. Run it on
any node of the cluster. [Documentation](isolated-net/)

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/domysh/pve-tools/main/isolated-net/install.sh)"
```

### gdrive-backup

Automatic upload of vzdump backups to Google Drive after every backup job
(rclone, optional encryption, Proxmox notifications), set up by a wizard. Run
it on the node that holds the backups. [Documentation](gdrive-backup/)

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/domysh/pve-tools/main/gdrive-backup/install.sh)"
```

## How the commands work

Each command downloads this repository from GitHub into a temporary
directory, runs the tool from there and deletes the download. What a tool
installs on the node (agents, services, hooks) is copied to its final place,
so nothing depends on the download afterwards.

* **Interactive**, as above: the tool shows what it is going to do and asks,
  starting from the current configuration when it is already installed. Run
  the same command again to change the configuration or to update the tool
  to the latest version.
* **Other commands and options** go after `--` (`bash -c` would take the
  first argument as the script name):

  ```bash
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/domysh/pve-tools/main/isolated-net/install.sh)" -- status
  ```

  `-- --help` lists the commands of a tool, `-- <command> --help` their
  options.
* **Without questions**: every question has an option that answers it, and
  `-y` asks nothing else, taking the current configuration or the defaults for
  the rest. Choices that cannot be made for you (which storage, which Google
  account, encryption...) must then be given as options, otherwise the tool
  stops with an error that names them. This makes a setup scriptable and
  reproducible on another node:

  ```bash
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/domysh/pve-tools/main/isolated-net/install.sh)" -- install -y --subnet4 10.100.0.0/22
  ```

  Without a terminal (`ssh node '...'` without `-t`, a script piped into
  `bash`) the tools cannot ask anything and refuse to run without `-y`.

`PVE_TOOLS_REF` (branch, tag or commit) and `PVE_TOOLS_REPO` (`owner/name`, for
a fork) choose what is downloaded, e.g. to try a branch before merging it:

```bash
PVE_TOOLS_REF=my-branch bash -c "$(curl -fsSL https://raw.githubusercontent.com/domysh/pve-tools/my-branch/isolated-net/install.sh)"
```

From a clone of the repository, `./<tool>/install.sh [arguments]` runs the local
copy instead of downloading one.

## Adding a tool

Create a new top-level directory named after the tool, containing at least a
`README.md` and an `install.sh` (copy one and change `tool`, `entry` and the
default command), and add its command to the list above. Its commands should
ask interactively, have an option for every question and a `-y` that asks
nothing. Keep tools independent: anything a tool needs at install time (config,
agents, unit files) lives inside its own directory.
