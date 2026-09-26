#!/usr/bin/env python3
"""
isolated-net-agent: keeps NAT, isolation and forwarding of Proxmox SDN
"isolated" zones in sync with the live state of a node.

Why an agent instead of the built-in SDN SNAT
---------------------------------------------
Proxmox renders its SNAT option as a fixed
``SNAT --to-source <node address> -o <egress interface>`` rule when the SDN
configuration is applied. If the node's uplink address or interface changes
(DHCP lease, new ISP prefix, different NIC), the rule silently breaks until
the next apply. This agent uses MASQUERADE instead, so the source address is
picked per connection from whatever interface the kernel routes through.

Isolation model
---------------
A guest on an isolated vnet may only reach destinations that the node would
send through a *default route*. Every more specific route present on the node
(the LAN, VPN overlays, other bridges such as a hand-made ``srvnet``) is
treated as "local" and rejected. On top of that come:

* a static list of special-purpose ranges (RFC 1918, CGNAT, ULA, ...), which
  also covers private networks that sit behind the LAN router and therefore
  never appear in the node's routing table;
* for every on-link global IPv6 prefix, its enclosing site prefix
  (``SITE_PREFIX_LEN6``, /48 by default), which covers the other /64s of the
  same ISP delegation that are routed by the LAN router.

All of this is recomputed on every route/link/address change, so nothing
depends on the current LAN addressing.

The agent is intentionally stateless: the only inputs are the shared config
file in /etc/pve (cluster-wide), the applied SDN configuration and the
kernel's routing table; the only outputs are one nftables table and a few
sysctls. Rules are replaced atomically and only when their content changes.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import logging
import os
import select
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence, Union

LOG = logging.getLogger("isolated-net-agent")

IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

SHARED_CONFIG_PATH = Path("/etc/pve/isolated-net.conf")
SDN_RUNNING_CONFIG_PATH = Path("/etc/pve/sdn/.running-config")
IPAM_STATE_PATH = Path("/etc/pve/sdn/pve-ipam-state.json")
DNSMASQ_LEASE_DIR = Path("/var/lib/misc")
LEASE_OWNERS_PATH = Path("/var/lib/isolated-net-agent/lease-owners.json")
IPV6_CONF_ROOT = Path("/proc/sys/net/ipv6/conf")
IPV4_FORWARD_PATH = Path("/proc/sys/net/ipv4/ip_forward")

NFT_FAMILY = "inet"
NFT_TABLE = "isolated_net"

# A full resync runs at least this often, even without netlink events, to pick
# up SDN applies and shared-config edits (pmxcfs does not support inotify).
POLL_INTERVAL_SECONDS = 30.0
# Netlink events arrive in bursts (e.g. ifreload touching every interface);
# wait until the burst settles before recomputing.
DEBOUNCE_SECONDS = 1.0

# Conntrack zone for packets entering Proxmox firewall bridges (fwbr*).
# This mirrors what Proxmox SDN installs for its own SNAT subnets: when
# br_netfilter is active (guest firewall enabled), a guest packet is seen by
# conntrack twice, first while bridged through fwbrX and again while routed
# by the vnet. Without separate zones, the bridged pass confirms the
# connection with a NAT null binding and the routed pass can no longer
# masquerade it. Without br_netfilter the rule never matches.
FIREWALL_BRIDGE_CT_ZONE = 1

DEFAULT_SITE_PREFIX_LEN6 = 48

STATIC_BLOCKED_V4: tuple[str, ...] = (
    "0.0.0.0/8",  # "this" network
    "10.0.0.0/8",  # RFC 1918
    "100.64.0.0/10",  # RFC 6598 shared/CGNAT space, used by most mesh VPNs
    "127.0.0.0/8",  # loopback
    "169.254.0.0/16",  # link-local
    "172.16.0.0/12",  # RFC 1918
    "192.0.0.0/24",  # IETF protocol assignments
    "192.0.2.0/24",  # TEST-NET-1
    "192.168.0.0/16",  # RFC 1918
    "198.18.0.0/15",  # benchmarking
    "198.51.100.0/24",  # TEST-NET-2
    "203.0.113.0/24",  # TEST-NET-3
    "224.0.0.0/4",  # multicast
    "240.0.0.0/4",  # reserved and limited broadcast
)

STATIC_BLOCKED_V6: tuple[str, ...] = (
    "::/128",  # unspecified
    "::1/128",  # loopback
    "::ffff:0:0/96",  # IPv4-mapped
    "100::/64",  # discard-only
    "2001:db8::/32",  # documentation
    "fc00::/7",  # unique local addresses
    "fe80::/10",  # link-local
    "ff00::/8",  # multicast
)


class AgentError(RuntimeError):
    """Raised for recoverable problems; the agent logs them and retries later."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def parse_key_value_file(path: Path) -> dict[str, str]:
    """Parse a shell-like ``KEY=VALUE`` file.

    Blank lines and ``#`` comments are ignored, and a value may be wrapped in
    single or double quotes. No shell expansion is performed, so the file is
    safe to read as root even though it lives on the shared cluster FS.
    """
    values: dict[str, str] = {}
    for number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            raise AgentError(f"{path}:{number}: expected KEY=VALUE, got {raw_line!r}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def split_list(value: str) -> list[str]:
    """Split a comma and/or whitespace separated list, dropping empty items."""
    return [item for item in value.replace(",", " ").split() if item]


@dataclass(frozen=True)
class AgentConfig:
    """Settings the agent needs; a subset of the deploy configuration."""

    zones: frozenset[str]
    site_prefix_len6: int
    extra_blocked: tuple[IPNetwork, ...]

    @classmethod
    def load(cls, path: Path) -> "AgentConfig":
        """Load the shared config, or return an empty config if it is missing."""
        if not path.exists():
            return cls(zones=frozenset(), site_prefix_len6=0, extra_blocked=())
        values = parse_key_value_file(path)

        raw_site_len = values.get("SITE_PREFIX_LEN6", str(DEFAULT_SITE_PREFIX_LEN6))
        try:
            site_prefix_len6 = int(raw_site_len)
        except ValueError as exc:
            raise AgentError(f"SITE_PREFIX_LEN6 must be an integer, got {raw_site_len!r}") from exc
        if not 0 <= site_prefix_len6 <= 64:
            raise AgentError("SITE_PREFIX_LEN6 must be between 0 (disabled) and 64")

        extra: list[IPNetwork] = []
        for item in split_list(values.get("EXTRA_BLOCKED", "")):
            try:
                extra.append(ipaddress.ip_network(item, strict=False))
            except ValueError as exc:
                raise AgentError(f"EXTRA_BLOCKED contains an invalid network: {item!r}") from exc

        return cls(
            zones=frozenset(split_list(values.get("ZONE", ""))),
            site_prefix_len6=site_prefix_len6,
            extra_blocked=tuple(extra),
        )


# --------------------------------------------------------------------------
# SDN topology (what is isolated)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Topology:
    """Vnets and subnets of the isolated zones, as currently *applied*."""

    vnets: frozenset[str]
    internal_v4: tuple[ipaddress.IPv4Network, ...]
    internal_v6: tuple[ipaddress.IPv6Network, ...]


def subnet_from_id(subnet_id: str) -> IPNetwork:
    """Convert an SDN subnet id (``<zone>-<network>-<prefixlen>``) to a network.

    Zone ids cannot contain dashes, so the first dash ends the zone and the
    last dash starts the prefix length; IPv6 networks keep their colons.
    """
    try:
        _zone, remainder = subnet_id.split("-", 1)
        network, prefix_len = remainder.rsplit("-", 1)
        return ipaddress.ip_network(f"{network}/{prefix_len}")
    except ValueError as exc:
        raise AgentError(f"unexpected SDN subnet id {subnet_id!r}") from exc


def load_topology(zones: frozenset[str], running_config_path: Path = SDN_RUNNING_CONFIG_PATH) -> Topology:
    """Extract isolated vnets and subnets from the applied SDN configuration.

    The *running* config is used on purpose instead of the editable
    ``*.cfg`` files: rules must match what ifupdown actually created, not
    changes that are still pending an apply.
    """
    if not zones or not running_config_path.exists():
        return Topology(frozenset(), (), ())
    try:
        running = json.loads(running_config_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentError(f"cannot read {running_config_path}: {exc}") from exc

    vnet_entries = (running.get("vnets") or {}).get("ids") or {}
    vnets = frozenset(name for name, entry in vnet_entries.items() if entry.get("zone") in zones)

    internal_v4: list[ipaddress.IPv4Network] = []
    internal_v6: list[ipaddress.IPv6Network] = []
    subnet_entries = (running.get("subnets") or {}).get("ids") or {}
    for subnet_id, entry in sorted(subnet_entries.items()):
        if entry.get("vnet") not in vnets:
            continue
        network = subnet_from_id(subnet_id)
        if isinstance(network, ipaddress.IPv4Network):
            internal_v4.append(network)
        else:
            internal_v6.append(network)

    return Topology(vnets, tuple(internal_v4), tuple(internal_v6))


# --------------------------------------------------------------------------
# Routing state (what is "local" and must be blocked)
# --------------------------------------------------------------------------


def run_command(args: Sequence[str], input_text: str | None = None) -> str:
    """Run a command and return stdout, raising AgentError on failure."""
    try:
        result = subprocess.run(
            list(args), input=input_text, capture_output=True, text=True, check=False, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentError(f"{' '.join(args)}: {exc}") from exc
    if result.returncode != 0:
        raise AgentError(f"{' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


def read_routes(version: int) -> list[dict]:
    """Return the main routing table of one address family as parsed JSON."""
    output = run_command(["ip", "-j", f"-{version}", "route", "show", "table", "main"])
    return json.loads(output) if output.strip() else []


def route_devices(route: dict) -> set[str]:
    """Return the egress device(s) of a route, including multipath nexthops."""
    devices = {route["dev"]} if route.get("dev") else set()
    for nexthop in route.get("nexthops") or []:
        if nexthop.get("dev"):
            devices.add(nexthop["dev"])
    return devices


def default_route_devices(routes: Iterable[dict]) -> set[str]:
    """Return the devices that carry a default route (the node's uplinks)."""
    devices: set[str] = set()
    for route in routes:
        if route.get("dst") == "default":
            devices |= route_devices(route)
    return devices


def compute_blocked(
    version: int,
    routes: Iterable[dict],
    topology: Topology,
    config: AgentConfig,
) -> list[IPNetwork]:
    """Compute the collapsed list of destinations isolated guests must not reach.

    Blocked = static special-purpose ranges
            + every non-default route of the node (except the isolated vnets)
            + the enclosing site prefix of every on-link global IPv6 network
            + EXTRA_BLOCKED from the shared config.

    Overlaps with the isolated subnets themselves are harmless: traffic that
    stays inside isolated vnets is accepted before the blocklist is checked.
    """
    static = STATIC_BLOCKED_V4 if version == 4 else STATIC_BLOCKED_V6
    blocked: list[IPNetwork] = [ipaddress.ip_network(item) for item in static]
    blocked.extend(net for net in config.extra_blocked if net.version == version)

    for route in routes:
        destination = route.get("dst")
        if not destination or destination == "default":
            continue
        if route_devices(route) & topology.vnets:
            continue
        try:
            network = ipaddress.ip_network(destination, strict=False)
        except ValueError:
            LOG.debug("ignoring route with unparsable destination %r", destination)
            continue
        if network.version != version or network.prefixlen == 0:
            continue
        blocked.append(network)

        # A LAN usually owns more than the /64 the node sits on (the rest of
        # an ISP delegation is routed by the LAN router and never shows up in
        # the node's routing table). Blocking the enclosing site prefix of
        # every *on-link* global network catches those without configuration.
        is_on_link = not route.get("gateway")
        if (
            version == 6
            and is_on_link
            and config.site_prefix_len6
            and network.is_global
            and network.prefixlen > config.site_prefix_len6
        ):
            blocked.append(network.supernet(new_prefix=config.site_prefix_len6))

    return list(ipaddress.collapse_addresses(blocked))  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Kernel settings
# --------------------------------------------------------------------------


def write_proc_value(path: Path, value: str) -> bool:
    """Write a /proc/sys value if it differs; return True when it changed.

    Writing only on change matters for ``all/forwarding``: every write resets
    the per-interface forwarding flags, even when the value is the same.
    """
    try:
        current = path.read_text().strip()
    except FileNotFoundError:
        return False
    if current == value:
        return False
    path.write_text(value)
    LOG.info("set %s = %s (was %s)", path, value, current)
    return True


def ensure_kernel_settings(uplinks: set[str], vnets: frozenset[str]) -> None:
    """Enable routing without breaking the node's own IPv6 autoconfiguration.

    Linux ignores router advertisements on interfaces that forward, unless
    ``accept_ra`` is 2. Uplinks are therefore switched from 1 to 2 *before*
    forwarding is enabled, otherwise a SLAAC-configured node would lose its
    IPv6 default route once the current one expires. An explicit 0 set by
    the administrator is left untouched.

    The isolated vnets get ``accept_ra = 0``: a guest must never be able to
    install routes on the node by sending its own router advertisements
    (the node would otherwise inherit ``default.accept_ra``, often 2).
    """
    for device in sorted(uplinks - vnets):
        accept_ra = IPV6_CONF_ROOT / device / "accept_ra"
        try:
            if accept_ra.read_text().strip() == "1":
                write_proc_value(accept_ra, "2")
        except FileNotFoundError:
            continue

    for device in sorted(vnets):
        write_proc_value(IPV6_CONF_ROOT / device / "accept_ra", "0")

    write_proc_value(IPV4_FORWARD_PATH, "1")
    write_proc_value(IPV6_CONF_ROOT / "all" / "forwarding", "1")


# --------------------------------------------------------------------------
# nftables ruleset
# --------------------------------------------------------------------------


def nft_set(name: str, element_type: str, elements: Sequence[str], interval: bool) -> str:
    """Render a named nftables set; empty sets are declared without elements."""
    lines = [f"\tset {name} {{", f"\t\ttype {element_type}"]
    if interval:
        lines.append("\t\tflags interval")
    if elements:
        lines.append(f"\t\telements = {{ {', '.join(elements)} }}")
    lines.append("\t}")
    return "\n".join(lines)


def render_ruleset(
    topology: Topology,
    blocked_v4: Sequence[IPNetwork],
    blocked_v6: Sequence[IPNetwork],
) -> str:
    """Render the complete, self-replacing nftables table.

    The leading ``table``/``delete table`` pair makes ``nft -f`` replace the
    table in one atomic transaction whether or not it already exists, so no
    packet ever sees a half-updated ruleset. Established connections survive
    because conntrack state lives outside the table.
    """
    vnet_elements = [f'"{name}"' for name in sorted(topology.vnets)]
    sets = "\n\n".join(
        [
            nft_set("isolated_ifaces", "ifname", vnet_elements, interval=False),
            nft_set("internal_v4", "ipv4_addr", [str(n) for n in topology.internal_v4], interval=True),
            nft_set("internal_v6", "ipv6_addr", [str(n) for n in topology.internal_v6], interval=True),
            nft_set("blocked_v4", "ipv4_addr", [str(n) for n in blocked_v4], interval=True),
            nft_set("blocked_v6", "ipv6_addr", [str(n) for n in blocked_v6], interval=True),
        ]
    )

    return f"""\
table {NFT_FAMILY} {NFT_TABLE}
delete table {NFT_FAMILY} {NFT_TABLE}

table {NFT_FAMILY} {NFT_TABLE} {{
\tcomment "managed by isolated-net-agent, do not edit"

{sets}

\t# See FIREWALL_BRIDGE_CT_ZONE in the agent source for the rationale.
\tchain raw_prerouting {{
\t\ttype filter hook prerouting priority raw; policy accept;
\t\tiifname "fwbr*" ct zone set {FIREWALL_BRIDGE_CT_ZONE}
\t}}

\t# Guests may only use the gateway for DHCP, router advertisements/NDP,
\t# ICMP and DNS (dnsmasq forwards to the node's own resolvers).
\tchain input {{
\t\ttype filter hook input priority filter; policy accept;
\t\tiifname @isolated_ifaces jump input_from_isolated
\t}}

\tchain input_from_isolated {{
\t\tct state established,related accept
\t\tct state invalid drop
\t\tmeta l4proto {{ icmp, ipv6-icmp }} accept
\t\tudp dport {{ 67, 547 }} accept
\t\tmeta l4proto {{ tcp, udp }} th dport 53 accept
\t\tcounter reject with icmpx admin-prohibited
\t}}

\t# Clamp TCP MSS to the route MTU (e.g. PPPoE uplinks), since guests are
\t# told the vnet MTU and path MTU discovery is often broken on the internet.
\tchain forward_mangle {{
\t\ttype filter hook forward priority mangle; policy accept;
\t\tiifname @isolated_ifaces tcp flags & (syn | rst) == syn tcp option maxseg size set rt mtu
\t\toifname @isolated_ifaces tcp flags & (syn | rst) == syn tcp option maxseg size set rt mtu
\t}}

\tchain forward {{
\t\ttype filter hook forward priority filter; policy accept;
\t\tiifname @isolated_ifaces jump forward_from_isolated
\t\toifname @isolated_ifaces jump forward_to_isolated
\t}}

\tchain forward_from_isolated {{
\t\t# Traffic staying inside isolated vnets (also bridged traffic when
\t\t# br_netfilter is active) is not subject to the blocklist.
\t\toifname @isolated_ifaces accept
\t\tct state established,related accept
\t\tct state invalid drop
\t\t# Anti-spoofing: only the vnet's own subnets may leave the node.
\t\tmeta nfproto ipv4 ip saddr != @internal_v4 counter drop
\t\tmeta nfproto ipv6 ip6 saddr != @internal_v6 counter drop
\t\tip daddr @blocked_v4 counter reject with icmpx admin-prohibited
\t\tip6 daddr @blocked_v6 counter reject with icmpx admin-prohibited
\t\taccept
\t}}

\t# Nothing outside the isolated zone may open connections into it, except
\t# explicit port forwards (DNAT rules an administrator adds elsewhere).
\tchain forward_to_isolated {{
\t\tct state established,related accept
\t\tct status dnat accept
\t\tcounter reject with icmpx admin-prohibited
\t}}

\t# MASQUERADE (not SNAT) so the source follows the current uplink address.
\tchain postrouting {{
\t\ttype nat hook postrouting priority srcnat; policy accept;
\t\toifname @isolated_ifaces return
\t\tip saddr @internal_v4 masquerade
\t\tip6 saddr @internal_v6 masquerade
\t}}
}}
"""


def nft_table_exists() -> bool:
    """Return True if the managed table is currently loaded."""
    result = subprocess.run(
        ["nft", "list", "table", NFT_FAMILY, NFT_TABLE], capture_output=True, text=True, check=False
    )
    return result.returncode == 0


def nft_apply(ruleset: str) -> None:
    """Load a ruleset atomically."""
    run_command(["nft", "-f", "-"], input_text=ruleset)


def nft_delete_table() -> bool:
    """Remove the managed table; return True if it existed."""
    if not nft_table_exists():
        return False
    run_command(["nft", "delete", "table", NFT_FAMILY, NFT_TABLE])
    return True


# --------------------------------------------------------------------------
# DHCP lease hygiene
# --------------------------------------------------------------------------
#
# Proxmox's dnsmasq integration only ever *adds* reservations (ethers file,
# plus a DBus AddDhcpLease for IPv4). Nothing removes leases when a guest is
# destroyed or its address is released, and leases are infinite. For IPv6
# this is a real problem: DHCPv6 leases are keyed by the client's DUID, not
# its MAC, so when the IPAM later hands the same address to another guest,
# dnsmasq still considers it leased to the old DUID and answers the new
# owner with "no addresses available" (the guest ends up link-local only).
# The agent therefore deletes, over dnsmasq's DBus API, every lease that no
# longer matches the IPAM.


@dataclass(frozen=True)
class Lease:
    """One entry of a dnsmasq lease file."""

    version: int
    ip: str  # canonical (compressed) form
    client: str  # lowercase MAC for IPv4, lowercase DUID for IPv6
    hostname: str


def canonical_ip(value: str) -> str:
    """Normalize an address so IPAM keys and lease entries compare equal."""
    return str(ipaddress.ip_address(value))


def load_ipam_reservations(zone: str, ipam_path: Path = IPAM_STATE_PATH) -> dict[str, str] | None:
    """Return ``{ip: lowercase MAC}`` of the guest reservations of a zone.

    Returns None when the zone is absent from the PVE IPAM state, so that a
    missing or unexpected file can never be mistaken for "no reservations"
    (which would delete every lease).
    """
    try:
        state = json.loads(ipam_path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentError(f"cannot read {ipam_path}: {exc}") from exc

    zone_state = ((state.get("zones") or {}).get(zone)) or None
    if zone_state is None:
        return None
    reservations: dict[str, str] = {}
    for subnet in (zone_state.get("subnets") or {}).values():
        for ip, entry in (subnet.get("ips") or {}).items():
            if entry.get("mac"):
                reservations[canonical_ip(ip)] = entry["mac"].lower()
    return reservations


def parse_lease_file(path: Path) -> list[Lease]:
    """Parse a dnsmasq lease file.

    IPv4 lines are ``<expiry> <mac> <ip> <hostname> <client-id>``; after the
    ``duid <server-duid>`` marker, IPv6 lines are
    ``<expiry> <iaid> <ip> <hostname> <client-duid>``.
    """
    leases: list[Lease] = []
    in_ipv6_section = False
    for line in path.read_text().splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] == "duid":
            in_ipv6_section = True
            continue
        if len(fields) < 5:
            continue
        try:
            if in_ipv6_section:
                leases.append(Lease(6, canonical_ip(fields[2]), fields[4].lower(), fields[3]))
            else:
                leases.append(Lease(4, canonical_ip(fields[2]), fields[1].lower(), fields[3]))
        except ValueError:
            LOG.debug("ignoring malformed lease line %r", line)
    return leases


def mac_from_duid(duid: str) -> str | None:
    """Extract the Ethernet MAC embedded in a DUID-LLT or DUID-LL, if any.

    DUID-LLT: type 1 (2 bytes), hardware type (2), time (4), address (6).
    DUID-LL:  type 3 (2 bytes), hardware type (2), address (6).
    DUID-EN (type 2, used by systemd-networkd) and DUID-UUID (type 4) carry
    no MAC; for those the agent relies on the ownership history instead.
    """
    octets = duid.split(":")
    if len(octets) < 4 or octets[2:4] != ["00", "01"]:  # hardware type 1 = Ethernet
        return None
    duid_type = octets[0] + octets[1]
    if duid_type == "0001" and len(octets) == 14:
        return ":".join(octets[8:])
    if duid_type == "0003" and len(octets) == 10:
        return ":".join(octets[4:])
    return None


def find_stale_leases(
    leases: Sequence[Lease],
    reservations: dict[str, str],
    known_owners: dict[str, str],
) -> tuple[list[Lease], dict[str, str]]:
    """Decide which leases no longer belong to the guest the IPAM reserves them for.

    A lease is stale when its address is no longer reserved, when its client
    MAC (IPv4, or IPv6 DUIDs that embed one) differs from the reserved MAC,
    or, for MAC-less DUIDs, when the reserved MAC changed since the lease was
    first seen. ``known_owners`` maps ``"<ip>|<duid>"`` to the MAC reserved
    at first sighting; the updated mapping is returned alongside the stale
    leases so it can be persisted.
    """
    stale: list[Lease] = []
    owners: dict[str, str] = {}
    for lease in leases:
        reserved_mac = reservations.get(lease.ip)
        if reserved_mac is None:
            stale.append(lease)
            continue
        if lease.version == 4:
            if lease.client != reserved_mac:
                stale.append(lease)
            continue
        embedded_mac = mac_from_duid(lease.client)
        if embedded_mac is not None:
            if embedded_mac != reserved_mac:
                stale.append(lease)
            continue
        key = f"{lease.ip}|{lease.client}"
        first_owner = known_owners.get(key, reserved_mac)
        if first_owner != reserved_mac:
            stale.append(lease)
        else:
            owners[key] = first_owner
    return stale, owners


def delete_dnsmasq_lease(zone: str, ip: str) -> None:
    """Delete a lease from the running dnsmasq@<zone> through its DBus API."""
    service = f"uk.org.thekelleys.dnsmasq.{zone}"
    run_command(["busctl", "call", service, "/uk/org/thekelleys/dnsmasq", service, "DeleteDhcpLease", "s", ip])


def read_lease_owners(path: Path = LEASE_OWNERS_PATH) -> dict[str, dict[str, str]]:
    """Load the persisted ``{zone: {"<ip>|<duid>": mac}}`` ownership history."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("ignoring unreadable %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def write_lease_owners(owners: dict[str, dict[str, str]], path: Path = LEASE_OWNERS_PATH) -> None:
    """Persist the ownership history atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(owners, indent=1, sort_keys=True))
    temporary.replace(path)


def clean_stale_leases(zones: Iterable[str]) -> None:
    """Delete leases of the isolated zones that no longer match the IPAM.

    The ownership history is persisted so a lease reassigned while the agent
    was not running is still detected on the next start.
    """
    history = read_lease_owners()
    updated: dict[str, dict[str, str]] = {}
    for zone in sorted(zones):
        lease_file = DNSMASQ_LEASE_DIR / f"dnsmasq.{zone}.leases"
        reservations = load_ipam_reservations(zone)
        if reservations is None or not lease_file.exists():
            if zone in history:
                updated[zone] = history[zone]
            continue

        stale, owners = find_stale_leases(parse_lease_file(lease_file), reservations, history.get(zone, {}))
        for lease in stale:
            try:
                delete_dnsmasq_lease(zone, lease.ip)
            except AgentError as exc:
                LOG.warning("could not delete stale lease %s (%s): %s", lease.ip, lease.client, exc)
                continue
            LOG.info(
                "deleted stale DHCPv%d lease %s of %s (%s) in zone %s",
                lease.version, lease.ip, lease.hostname, lease.client, zone,
            )
        updated[zone] = owners

    if updated != history:
        write_lease_owners(updated)


# --------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DesiredState:
    """Everything one sync derives from its inputs."""

    config: AgentConfig
    topology: Topology
    uplinks: set[str]
    ruleset: str | None  # None means "no isolated zone configured"


def compute_desired_state(config_path: Path) -> DesiredState:
    """Read all inputs and compute the ruleset without touching the system."""
    config = AgentConfig.load(config_path)
    if not config.zones:
        return DesiredState(config, Topology(frozenset(), (), ()), set(), None)

    topology = load_topology(config.zones)
    routes_v4 = read_routes(4)
    routes_v6 = read_routes(6)
    uplinks = default_route_devices(routes_v4) | default_route_devices(routes_v6)
    ruleset = render_ruleset(
        topology,
        compute_blocked(4, routes_v4, topology, config),
        compute_blocked(6, routes_v6, topology, config),
    )
    return DesiredState(config, topology, uplinks, ruleset)


class Agent:
    """Event-driven reconciliation loop."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.applied_digest: str | None = None
        self.force_sync = True
        self.stop_requested = False

    def sync(self) -> None:
        """Reconcile kernel settings and nftables with the desired state."""
        state = compute_desired_state(self.config_path)
        if state.ruleset is None:
            if nft_delete_table():
                LOG.info("no isolated zone configured: removed table %s", NFT_TABLE)
            self.applied_digest = None
            return

        ensure_kernel_settings(state.uplinks, state.topology.vnets)
        self.sync_ruleset(state)

        # Lease hygiene is best effort and must never block NAT/isolation.
        try:
            clean_stale_leases(state.config.zones)
        except (AgentError, OSError, ValueError) as exc:
            LOG.warning("DHCP lease cleanup failed: %s", exc)

    def sync_ruleset(self, state: DesiredState) -> None:
        """Load the rendered ruleset if it changed or the table disappeared."""
        assert state.ruleset is not None
        digest = hashlib.sha256(state.ruleset.encode()).hexdigest()
        if digest == self.applied_digest and nft_table_exists():
            return
        nft_apply(state.ruleset)
        self.applied_digest = digest
        LOG.info(
            "applied ruleset: vnets=%s internal=%s uplinks=%s",
            ",".join(sorted(state.topology.vnets)) or "-",
            ",".join(str(n) for n in state.topology.internal_v4 + state.topology.internal_v6) or "-",
            ",".join(sorted(state.uplinks)) or "-",
        )

    def safe_sync(self) -> None:
        """Run a sync, logging (instead of propagating) recoverable errors.

        On failure the previous ruleset stays loaded (fail-static), which
        keeps guests isolated while e.g. pmxcfs is temporarily unavailable.
        """
        try:
            self.sync()
        except (AgentError, OSError, ValueError) as exc:
            LOG.error("sync failed, keeping previous state: %s", exc)
            self.applied_digest = None  # retry on the next tick

    def run(self) -> None:
        """Main loop: resync on netlink events, SIGHUP and a periodic timer."""
        wakeup_read, wakeup_write = os.pipe()
        os.set_blocking(wakeup_read, False)
        os.set_blocking(wakeup_write, False)
        # The wakeup fd interrupts select() immediately on any signal, so
        # SIGHUP/SIGTERM are handled without waiting for the next timeout.
        signal.set_wakeup_fd(wakeup_write)
        signal.signal(signal.SIGHUP, self._on_reload)
        signal.signal(signal.SIGTERM, self._on_stop)
        signal.signal(signal.SIGINT, self._on_stop)

        monitor: subprocess.Popen | None = None
        next_poll = 0.0
        pending_since: float | None = None

        LOG.info("started (config %s)", self.config_path)
        while not self.stop_requested:
            if monitor is None or monitor.poll() is not None:
                monitor = self._start_monitor()
                pending_since = time.monotonic()  # events may have been missed

            now = time.monotonic()
            if self.force_sync or now >= next_poll or (
                pending_since is not None and now - pending_since >= DEBOUNCE_SECONDS
            ):
                self.force_sync = False
                pending_since = None
                next_poll = now + POLL_INTERVAL_SECONDS
                self.safe_sync()
                continue

            deadline = next_poll
            if pending_since is not None:
                deadline = min(deadline, pending_since + DEBOUNCE_SECONDS)
            timeout = max(0.0, deadline - now)

            assert monitor.stdout is not None
            readable, _, _ = select.select([monitor.stdout, wakeup_read], [], [], timeout)
            if wakeup_read in readable:
                self._drain(wakeup_read)
            if monitor.stdout in readable:
                chunk = os.read(monitor.stdout.fileno(), 65536)
                if not chunk:
                    LOG.warning("ip monitor exited, restarting it")
                    monitor.wait()
                    monitor = None
                elif pending_since is None:
                    pending_since = time.monotonic()

        if monitor is not None and monitor.poll() is None:
            monitor.terminate()
            monitor.wait(timeout=5)
        LOG.info("stopped (ruleset left in place)")

    @staticmethod
    def _start_monitor() -> subprocess.Popen:
        """Watch netlink for anything that can change routes or uplinks."""
        return subprocess.Popen(
            ["ip", "-o", "monitor", "link", "address", "route"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    @staticmethod
    def _drain(fd: int) -> None:
        try:
            while os.read(fd, 512):
                pass
        except BlockingIOError:
            pass

    def _on_reload(self, _signum: int, _frame: object) -> None:
        self.force_sync = True
        self.applied_digest = None

    def _on_stop(self, _signum: int, _frame: object) -> None:
        self.stop_requested = True


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--config", type=Path, default=SHARED_CONFIG_PATH, help="shared config file")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=("run", "sync", "show", "cleanup"),
        help="run: daemon (default); sync: reconcile once; "
        "show: print the ruleset without applying; cleanup: remove the table",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stdout,
    )

    try:
        if args.command == "show":
            state = compute_desired_state(args.config)
            print(state.ruleset if state.ruleset else "# no isolated zone configured")
        elif args.command == "cleanup":
            removed = nft_delete_table()
            LOG.info("table %s %s", NFT_TABLE, "removed" if removed else "was not loaded")
        elif args.command == "sync":
            Agent(args.config).sync()
        else:
            Agent(args.config).run()
    except AgentError as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
