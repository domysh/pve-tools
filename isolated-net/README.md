# isolated-net

An isolated, internet-only, dual-stack guest network for Proxmox VE, built on
SDN and reproducible with one command.

* **Isolated**: guests cannot reach the LAN, the node itself (except DHCP, DNS,
  ICMP), VPN overlays, other bridges, or anything else that is "local" to the
  node. Nothing outside can open connections into the network.
* **Internet access** over IPv4 and IPv6 via MASQUERADE, following whatever
  uplink/address the node currently has.
* **Automatic addressing**: DHCPv4, DHCPv6 and router advertisements from the
  SDN dnsmasq backend.
* **Cluster-unique, migration-safe addresses**: the PVE IPAM (stored in
  `/etc/pve`) binds each address to the guest's MAC; the gateway exists on every
  node, so a migrated guest keeps its addresses and its default route.
* **No hardcoded LAN details**: what counts as "local" is derived from the
  node's routing table at runtime and updated on every route change.

## How it works

```
            guest (DHCPv4 + DHCPv6/RA from dnsmasq@<zone>)
                 |
   vnet "isonet" (SDN Simple zone, anycast gateway on every node)
                 |
      node: table inet isolated_net  (isolated-net-agent)
        - from vnet: reject if destination is in blocked_v4/blocked_v6
        - to vnet:   only established/related (and explicit DNAT)
        - input:     only DHCP, DNS, ICMP to the gateway
        - postrouting: masquerade IPv4 + IPv6
                 |
          default route -> internet
```

Two parts:

1. **SDN objects** (`deploy.py`): a Simple zone with `ipam=pve` and
   `dhcp=dnsmasq`, one vnet, one IPv4 subnet and one ULA IPv6 subnet with DHCP
   ranges. SDN's own SNAT option is deliberately **not** used: Proxmox renders
   it as `SNAT --to-source <node IP> -o <iface>` at apply time, which breaks as
   soon as the node's address or uplink changes.
2. **isolated-net-agent** (`agent/isolated_net_agent.py`, systemd service on
   every node): owns one nftables table and keeps it in sync with the applied
   SDN config and the routing table. It reacts to `ip monitor` events and
   resyncs every 30 s; updates are atomic and only happen when the rendered
   ruleset changes.

### What is blocked

A guest may reach a destination only if the node would send it through a
**default route**. The blocklist is:

* every non-default route on the node, except the isolated vnets themselves
  (LAN subnets, VPN overlays such as NetBird, other bridges such as a manual
  `srvnet`, container networks, ...);
* static special-purpose ranges: RFC 1918, 100.64.0.0/10, link-local, ULA
  (`fc00::/7`), multicast, documentation ranges, ...;
* for every on-link global IPv6 prefix, its enclosing `/SITE_PREFIX_LEN6`
  (default `/48`): the rest of your ISP delegation, routed by the LAN router;
* `EXTRA_BLOCKED` from the config.

Plus anti-spoofing (only the vnet subnets may leave the node) and TCP MSS
clamping to the route MTU (useful with PPPoE uplinks).

### DHCP lease hygiene

Proxmox's dnsmasq integration only ever adds reservations: nothing removes a
lease when a guest is destroyed or its address is released, and leases never
expire. For IPv6 that breaks address reuse, because DHCPv6 leases belong to the
client's DUID rather than its MAC: when the IPAM hands a freed address to
another guest, dnsmasq still considers it leased to the old client and answers
"no addresses available", leaving the new guest with only a link-local
address. The agent deletes (through dnsmasq's DBus API) every lease whose
address is no longer reserved, whose MAC differs from the reservation, or,
for DUIDs without a MAC such as systemd-networkd's, whose reservation changed
owner since the lease was first seen (history kept in
`/var/lib/isolated-net-agent`).

The agent also enables IPv4/IPv6 forwarding, first switching the uplinks'
`accept_ra` from 1 to 2 so the node keeps its own SLAAC default route. This
matters beyond the first run: every SDN apply (`ifreload -a`) resets
`accept_ra` to 1, and a forwarding node with `accept_ra=1` silently loses its
IPv6 default route once the router lifetime expires; the agent restores it
within a second. The isolated vnets get `accept_ra=0` so guests can never
inject routes into the node with rogue router advertisements.

## Deploy

From your workstation (needs root SSH to one node of the cluster); the
`./remote-deploy.sh` commands in this README run from this directory:

```bash
cd isolated-net                          # from the repository root
./remote-deploy.sh root@node1            # install / reconcile
./remote-deploy.sh root@node1 status     # subnets, agents, IPAM allocations
```

Or directly on a node, from a clone of the repository (see the main README):

```bash
cd /root/pve-tools/isolated-net
python3 deploy.py install
```

`install` is idempotent. It installs `dnsmasq` on every node (without ever
starting its default instance), creates or reconciles the zone, vnet and
subnets, stores the resolved settings in `/etc/pve/isolated-net.conf`, installs
the agent on every online node and applies the SDN config only if something is
pending. After adding a node to the cluster, run it again.

### Configuration

Edit `isolated-net.conf` before the first run:

| Key | Default | Meaning |
| --- | --- | --- |
| `ZONE`, `VNET` | `isolated`, `isonet` | SDN ids (max 8 chars) |
| `SUBNET4` | `auto` | first free `/22` from `10.100.0.0` |
| `SUBNET6` | `auto` | random RFC 4193 ULA `/48`, subnet 1 as `/64` |
| `DNS4`, `DNS6` | empty | empty = the gateway (dnsmasq forwards to the node's resolvers) |
| `SITE_PREFIX_LEN6` | `48` | enclosing prefix blocked around on-link global IPv6 networks |
| `EXTRA_BLOCKED` | empty | more CIDRs to block |

`auto` subnets are chosen once. Afterwards the SDN config is the source of
truth and `/etc/pve/isolated-net.conf` records the resolved values: copy them
into `isolated-net.conf` to recreate the identical network elsewhere.

## Using the network

Attach a NIC to bridge `isonet`; the IPAM allocates an IPv4 and an IPv6
address when the guest is created/started.

```bash
# VM
qm set <vmid> --net1 virtio,bridge=isonet
# VM with cloud-init
qm set <vmid> --ipconfig1 ip=dhcp,ip6=dhcp
# LXC
pct set <vmid> --net1 name=eth1,bridge=isonet,ip=dhcp,ip6=dhcp
```

Inside the guest just use DHCP (IPv4) and DHCPv6 + RA (IPv6). To see or pin
allocations: Datacenter > SDN > IPAM, or `./remote-deploy.sh root@node1 status`.

**LXC and DNS:** containers without an explicit `nameserver` inherit the
node's `/etc/resolv.conf`, which usually points at something the network
blocks (the LAN router, a VPN resolver). Point them at the gateway, which
forwards to the node's resolvers (gateways are listed in
`/etc/pve/isolated-net.conf`, `.1` and `::1` of each subnet):

```bash
pct set <vmid> --nameserver "<IPv4 gateway> <IPv6 gateway>"
```

VMs are not affected: they take the DNS server from DHCP.

## Operations

```bash
journalctl -u isolated-net-agent -f     # agent log
isolated-net-agent show                 # ruleset the agent would apply
nft list table inet isolated_net        # live ruleset and counters
systemctl reload isolated-net-agent     # force a resync
```

Uninstall:

```bash
./remote-deploy.sh root@node1 uninstall            # agent only: the vnet stays, without NAT/isolation
./remote-deploy.sh root@node1 uninstall --purge    # also subnets, vnet, zone
```

`--purge` refuses to run while a guest still uses the vnet.

## Notes and limits

* **Simple zones are node-local layer 2.** Guests keep their addresses when
  migrated, but guests on *different* nodes cannot talk to each other directly.
  That would need an EVPN zone, for which Proxmox does not implement DHCP yet.
* **ULA and address selection.** Per RFC 6724 most OSes prefer IPv4 over a
  ULA source when the destination is a global IPv6 address, so dual-stack
  guests will use IPv4 for most internet traffic. That is expected; IPv6 works
  when chosen explicitly (e.g. `curl -6`) or with a tweaked `gai.conf`.
* **Port forwards** into the network are allowed by the agent as long as they
  are DNAT rules (`ct status dnat accept`); add them in your own nftables table.
* **Coexistence.** The agent only touches its own table; iptables-legacy rules
  (e.g. NetBird, `srvnet`) keep working side by side.
