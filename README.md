# pve-tools

A collection of tools for Proxmox VE. Each tool lives in its own directory,
is self-contained (its own scripts, configuration and README) and can be
deployed independently of the others.

## Tools

| Tool | Description |
| --- | --- |
| [isolated-net](isolated-net/) | Isolated, internet-only, dual-stack guest network built on SDN, with an agent that keeps NAT and isolation in sync with the node's routing table |
| [gdrive-backup](gdrive-backup/) | Automatic upload of vzdump backups to Google Drive after every backup job (rclone, optional encryption, Proxmox notifications), set up by an interactive wizard |

See each tool's README for how to configure, deploy and operate it.

## Adding a tool

Create a new top-level directory named after the tool, containing at least a
`README.md`, and add it to the table above. Keep tools independent: anything a
tool needs at deploy time (config, agents, unit files) lives inside its own
directory.
