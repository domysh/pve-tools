# pve-tools

A collection of tools for Proxmox VE. Each tool lives in its own directory,
is self-contained (its own scripts, configuration and README) and can be
deployed independently of the others.

> **Personal tools.** I wrote these for my own Proxmox VE setup and share them
> as they are, in case they help someone else. They follow my needs, may change
> or break without notice and come with no support or warranty: read the code
> and try them on a test node before trusting them with your data.

## Tools

| Tool | Description |
| --- | --- |
| [isolated-net](isolated-net/) | Isolated, internet-only, dual-stack guest network built on SDN, with an agent that keeps NAT and isolation in sync with the node's routing table |
| [gdrive-backup](gdrive-backup/) | Automatic upload of vzdump backups to Google Drive after every backup job (rclone, optional encryption, Proxmox notifications), set up by an interactive wizard |

See each tool's README for how to configure, deploy and operate it.

## Deploying

Every tool has a `remote-deploy.sh` that runs it on a node over root SSH:

```bash
./<tool>/remote-deploy.sh root@node1 [arguments]
```

It keeps a clone of this whole repository in `/root/pve-tools` on the node
(cloned over HTTPS the first time, `git pull` on every later run) and runs the
tool from there. The node gets what is on GitHub, so push your changes first:
the script warns when the local repository has changes that are not pushed.

To work directly on a node instead:

```bash
git clone https://github.com/domysh/pve-tools.git /root/pve-tools
```

## Adding a tool

Create a new top-level directory named after the tool, containing at least a
`README.md` and a `remote-deploy.sh`, and add it to the table above. Keep tools
independent: anything a tool needs at deploy time (config, agents, unit files)
lives inside its own directory.
