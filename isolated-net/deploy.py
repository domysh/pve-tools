#!/usr/bin/env python3
"""
Idempotent deployment of an isolated, internet-only, dual-stack network on
Proxmox VE SDN.

What it builds
--------------
* A Simple SDN zone using the built-in PVE IPAM and the dnsmasq DHCP backend.
  The IPAM lives in /etc/pve, so every address is unique cluster-wide and is
  bound to the guest's MAC address: a guest keeps its IPv4/IPv6 addresses when
  it is migrated, and the anycast gateway exists on every node.
* One vnet with an IPv4 subnet and a ULA IPv6 subnet, both served by DHCP
  (DHCPv4, DHCPv6 + router advertisements).
* isolated-net-agent on every node: MASQUERADE for both families and a
  blocklist derived from the node's routing table (see the agent docstring).

Why SDN's own SNAT option is *not* used
---------------------------------------
Proxmox renders it as ``SNAT --to-source <node IP> -o <iface>`` at apply time,
which breaks as soon as the node's LAN address or uplink changes. The agent
masquerades instead and recomputes everything on route changes.

Every step converges: existing objects are reused, "auto" subnets are chosen
only once (afterwards the SDN config is the source of truth) and the SDN
configuration is applied only when something is pending.

Usage (as root on any cluster node)::

    python3 deploy.py install [--config isolated-net.conf]
    python3 deploy.py status
    python3 deploy.py uninstall [--purge]
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence, Union

IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

TOOL_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = TOOL_DIR / "isolated-net.conf"
AGENT_SOURCE = TOOL_DIR / "agent" / "isolated_net_agent.py"
UNIT_SOURCE = TOOL_DIR / "agent" / "isolated-net-agent.service"

SHARED_CONFIG_PATH = Path("/etc/pve/isolated-net.conf")
AGENT_TARGET = "/usr/local/sbin/isolated-net-agent"
UNIT_TARGET = "/etc/systemd/system/isolated-net-agent.service"
SERVICE_NAME = "isolated-net-agent"

SDN_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]{1,7}$")
AUTO_IPV4_POOL = ipaddress.ip_network("10.0.0.0/8")
AUTO_IPV4_START = ipaddress.ip_address("10.100.0.0")
# Addresses below this offset stay out of the DHCP range, for static use.
DHCP_V4_FIRST_OFFSET = 10
# DHCPv6 hands out ::1000-::ffff; the low part stays free for static use.
DHCP_V6_FIRST_OFFSET = 0x1000
DHCP_V6_LAST_OFFSET = 0xFFFF
SDN_APPLY_TIMEOUT_SECONDS = 300


class DeployError(RuntimeError):
    """A fatal, user-facing error."""


def log(message: str) -> None:
    print(f"==> {message}", flush=True)


# --------------------------------------------------------------------------
# Command helpers
# --------------------------------------------------------------------------


def run(args: Sequence[str], input_text: str | None = None, check: bool = True) -> str:
    """Run a command, returning stdout; raise DeployError with stderr on failure."""
    result = subprocess.run(list(args), input=input_text, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise DeployError(f"command failed: {shlex.join(args)}\n{details}")
    return result.stdout


def pvesh(method: str, path: str, **params: Any) -> Any:
    """Call the Proxmox API through pvesh and decode its JSON output.

    Keyword arguments become ``--key value`` options; underscores map to
    dashes and list values repeat the option (as array parameters require).
    """
    args = ["pvesh", method, path, "--output-format", "json"]
    for key, value in params.items():
        if value is None:
            continue
        option = "--" + key.replace("_", "-")
        for item in value if isinstance(value, list) else [value]:
            args += [option, str(item)]
    output = run(args).strip()
    if not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        # Calls that start a worker task (e.g. applying SDN) make pvesh wait
        # for it and stream the task log first; the JSON result is last.
        return json.loads(output.splitlines()[-1])


@dataclass(frozen=True)
class Node:
    """A cluster member and how to reach it."""

    name: str
    address: str | None
    is_local: bool

    def run_script(self, script: str) -> str:
        """Run a bash script on this node (locally or over the cluster's SSH)."""
        if self.is_local:
            return run(["bash", "-s"], input_text=script)
        target = f"root@{self.address or self.name}"
        return run(["ssh", "-o", "BatchMode=yes", target, "bash", "-s"], input_text=script)


def local_node_name() -> str:
    """Proxmox node names are the short hostname."""
    return socket.gethostname().split(".", 1)[0]


def cluster_nodes() -> list[Node]:
    """Return every online node, the local one first."""
    local = local_node_name()
    addresses: dict[str, str] = {}
    for entry in pvesh("get", "/cluster/status") or []:
        if entry.get("type") == "node" and entry.get("ip"):
            addresses[entry["name"]] = entry["ip"]

    nodes = []
    for entry in pvesh("get", "/nodes") or []:
        name = entry["node"]
        if entry.get("status") not in (None, "online"):
            log(f"WARNING: node {name} is {entry.get('status')}, skipping it (re-run later)")
            continue
        nodes.append(Node(name=name, address=addresses.get(name), is_local=(name == local)))
    nodes.sort(key=lambda node: (not node.is_local, node.name))
    if not nodes:
        raise DeployError("no online Proxmox node found")
    return nodes


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def parse_key_value_file(path: Path) -> dict[str, str]:
    """Parse a shell-like KEY=VALUE file without any shell expansion."""
    values: dict[str, str] = {}
    for number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise DeployError(f"{path}:{number}: expected KEY=VALUE")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


@dataclass(frozen=True)
class Settings:
    """Validated deployment settings."""

    zone: str
    vnet: str
    vnet_alias: str
    subnet4: ipaddress.IPv4Network | None  # None means "auto"
    subnet4_auto_prefix_len: int
    subnet6: ipaddress.IPv6Network | None  # None means "auto"
    dns4: ipaddress.IPv4Address | None
    dns6: ipaddress.IPv6Address | None
    site_prefix_len6: int
    extra_blocked: tuple[IPNetwork, ...]

    @classmethod
    def load(cls, path: Path) -> "Settings":
        values = parse_key_value_file(path)

        def get(key: str, default: str = "") -> str:
            return values.get(key, default).strip()

        zone, vnet = get("ZONE", "isolated"), get("VNET", "isonet")
        for key, value in (("ZONE", zone), ("VNET", vnet)):
            if not SDN_ID_PATTERN.match(value):
                raise DeployError(f"{key}={value!r}: use 2-8 lowercase letters/digits, starting with a letter")

        def optional_network(key: str, version: int) -> Any:
            value = get(key, "auto")
            if value.lower() == "auto":
                return None
            try:
                network = ipaddress.ip_network(value, strict=True)
            except ValueError as exc:
                raise DeployError(f"{key}: {exc}") from exc
            if network.version != version:
                raise DeployError(f"{key} must be an IPv{version} network")
            return network

        def optional_address(key: str, version: int) -> Any:
            value = get(key)
            if not value:
                return None
            try:
                address = ipaddress.ip_address(value)
            except ValueError as exc:
                raise DeployError(f"{key}: {exc}") from exc
            if address.version != version:
                raise DeployError(f"{key} must be an IPv{version} address")
            return address

        def integer(key: str, default: int, low: int, high: int) -> int:
            try:
                number = int(get(key, str(default)))
            except ValueError as exc:
                raise DeployError(f"{key} must be an integer") from exc
            if not low <= number <= high:
                raise DeployError(f"{key} must be between {low} and {high}")
            return number

        subnet4 = optional_network("SUBNET4", 4)
        if subnet4 is not None and subnet4.prefixlen > 29:
            raise DeployError("SUBNET4 must be /29 or larger")
        subnet6 = optional_network("SUBNET6", 6)
        if subnet6 is not None and not 48 <= subnet6.prefixlen <= 112:
            raise DeployError("SUBNET6 must be between /48 and /112 (a /64 is recommended)")

        extra: list[IPNetwork] = []
        for item in get("EXTRA_BLOCKED").replace(",", " ").split():
            try:
                extra.append(ipaddress.ip_network(item, strict=False))
            except ValueError as exc:
                raise DeployError(f"EXTRA_BLOCKED: {exc}") from exc

        return cls(
            zone=zone,
            vnet=vnet,
            vnet_alias=get("VNET_ALIAS", "Isolated NAT network"),
            subnet4=subnet4,
            subnet4_auto_prefix_len=integer("SUBNET4_AUTO_PREFIX_LEN", 22, 16, 29),
            subnet6=subnet6,
            dns4=optional_address("DNS4", 4),
            dns6=optional_address("DNS6", 6),
            site_prefix_len6=integer("SITE_PREFIX_LEN6", 48, 0, 64),
            extra_blocked=tuple(extra),
        )


# --------------------------------------------------------------------------
# Address selection
# --------------------------------------------------------------------------


def subnet_cidr(entry: dict) -> IPNetwork:
    """Return the network of an SDN subnet API entry (``<zone>-<net>-<len>`` id)."""
    if entry.get("cidr"):
        return ipaddress.ip_network(entry["cidr"], strict=False)
    _zone, remainder = entry["subnet"].split("-", 1)
    network, prefix_len = remainder.rsplit("-", 1)
    return ipaddress.ip_network(f"{network}/{prefix_len}")


def all_sdn_subnets() -> list[IPNetwork]:
    """Every subnet defined in SDN, including pending (not yet applied) ones."""
    networks: list[IPNetwork] = []
    for vnet in pvesh("get", "/cluster/sdn/vnets") or []:
        for entry in pvesh("get", f"/cluster/sdn/vnets/{vnet['vnet']}/subnets") or []:
            networks.append(subnet_cidr(entry))
    return networks


NODE_NETWORKS_SCRIPT = r"""
ip -j route show table all
echo '@@@'
ip -j -6 route show table all
echo '@@@'
ip -j addr show
"""


def node_used_networks(node: Node) -> list[IPNetwork]:
    """Networks that are routed or configured on a node (all routing tables)."""
    route_v4, route_v6, addresses = node.run_script(NODE_NETWORKS_SCRIPT).split("@@@\n")
    networks: list[IPNetwork] = []
    for route in json.loads(route_v4) + json.loads(route_v6):
        destination = route.get("dst")
        if not destination or destination == "default":
            continue
        if route.get("type") in ("local", "broadcast", "multicast", "anycast"):
            continue
        try:
            networks.append(ipaddress.ip_network(destination, strict=False))
        except ValueError:
            continue
    for interface in json.loads(addresses):
        for info in interface.get("addr_info", []):
            try:
                networks.append(ipaddress.ip_interface(f"{info['local']}/{info['prefixlen']}").network)
            except (KeyError, ValueError):
                continue
    return networks


def pick_free_ipv4(prefix_len: int, used: Iterable[IPNetwork]) -> ipaddress.IPv4Network:
    """Return the first /prefix_len inside 10.0.0.0/8, from 10.100.0.0 up, that overlaps nothing.

    A deterministic scan (rather than a random pick) keeps the result stable
    and easy to remember; it runs only once, since existing subnets are reused.
    """
    used_v4 = [net for net in used if net.version == 4 and net.prefixlen > 0]
    block = 2 ** (32 - prefix_len)
    start = int(AUTO_IPV4_START) - (int(AUTO_IPV4_START) % block)
    for base in range(start, int(AUTO_IPV4_POOL.broadcast_address) + 1, block):
        candidate = ipaddress.IPv4Network((base, prefix_len))
        if not any(candidate.overlaps(net) for net in used_v4):
            return candidate
    raise DeployError("no free IPv4 subnet in 10.100.0.0-10.255.255.255, set SUBNET4 explicitly")


def generate_ula_subnet() -> ipaddress.IPv6Network:
    """Generate ``fdXX:XXXX:XXXX:1::/64`` from a random RFC 4193 global ID.

    RFC 4193 section 3.2.2 derives the 40-bit global ID from a SHA-1 over a
    timestamp and a machine identifier; extra randomness is mixed in so two
    clusters installed at the same moment still differ.
    """
    try:
        machine_id = Path("/etc/machine-id").read_bytes()
    except OSError:
        machine_id = b""
    digest = hashlib.sha1(time.time_ns().to_bytes(8, "big") + machine_id + secrets.token_bytes(16)).digest()
    global_id = int.from_bytes(digest[-5:], "big")
    prefix = (0xFD << 120) | (global_id << 80) | (1 << 64)  # subnet ID 1
    return ipaddress.IPv6Network((prefix, 64))


def dhcp_range(network: IPNetwork) -> str:
    """Return the ``dhcp-range`` property value for a subnet."""
    if network.version == 4:
        # Tiny subnets cannot afford a static block: start right after the gateway.
        offset = DHCP_V4_FIRST_OFFSET if network.num_addresses >= 64 else 2
        first = network.network_address + offset
        last = network.broadcast_address - 1
    else:
        first = network.network_address + min(DHCP_V6_FIRST_OFFSET, network.num_addresses // 2)
        last = network.network_address + min(DHCP_V6_LAST_OFFSET, network.num_addresses - 2)
    return f"start-address={first},end-address={last}"


# --------------------------------------------------------------------------
# SDN objects
# --------------------------------------------------------------------------


def ensure_zone(settings: Settings) -> None:
    zones = {entry["zone"]: entry for entry in pvesh("get", "/cluster/sdn/zones") or []}
    existing = zones.get(settings.zone)
    if existing is None:
        log(f"creating simple zone {settings.zone} (IPAM pve, DHCP dnsmasq)")
        pvesh("create", "/cluster/sdn/zones", zone=settings.zone, type="simple", ipam="pve", dhcp="dnsmasq")
        return
    if existing.get("type") != "simple":
        raise DeployError(f"zone {settings.zone} exists with type {existing.get('type')}, expected simple")
    if existing.get("ipam") != "pve" or existing.get("dhcp") != "dnsmasq":
        log(f"zone {settings.zone}: enabling IPAM pve and DHCP dnsmasq")
        pvesh("set", f"/cluster/sdn/zones/{settings.zone}", ipam="pve", dhcp="dnsmasq")
    else:
        log(f"zone {settings.zone} already present")


def ensure_vnet(settings: Settings) -> None:
    vnets = {entry["vnet"]: entry for entry in pvesh("get", "/cluster/sdn/vnets") or []}
    existing = vnets.get(settings.vnet)
    if existing is None:
        log(f"creating vnet {settings.vnet}")
        pvesh("create", "/cluster/sdn/vnets", vnet=settings.vnet, zone=settings.zone, alias=settings.vnet_alias)
        return
    if existing.get("zone") != settings.zone:
        raise DeployError(f"vnet {settings.vnet} already belongs to zone {existing.get('zone')}")
    log(f"vnet {settings.vnet} already present")


def ensure_subnets(settings: Settings, nodes: Sequence[Node]) -> tuple[IPNetwork, IPNetwork]:
    """Make sure the vnet has exactly one IPv4 and one IPv6 subnet with DHCP."""
    path = f"/cluster/sdn/vnets/{settings.vnet}/subnets"
    existing = {4: [], 6: []}  # type: dict[int, list[IPNetwork]]
    for entry in pvesh("get", path) or []:
        network = subnet_cidr(entry)
        existing[network.version].append(network)

    result: dict[int, IPNetwork] = {}
    for version, requested in ((4, settings.subnet4), (6, settings.subnet6)):
        current = existing[version]
        if current:
            if requested is not None and requested not in current:
                raise DeployError(
                    f"vnet {settings.vnet} already has IPv{version} subnet {current[0]} but the config asks for "
                    f"{requested}; renumbering is not automatic (delete the subnet first)"
                )
            log(f"IPv{version} subnet {current[0]} already present")
            result[version] = current[0]
            continue

        if requested is not None:
            network = requested
        elif version == 4:
            used = all_sdn_subnets()
            for node in nodes:
                used += node_used_networks(node)
            network = pick_free_ipv4(settings.subnet4_auto_prefix_len, used)
        else:
            network = generate_ula_subnet()

        gateway = network.network_address + 1
        dns = settings.dns4 if version == 4 else settings.dns6
        log(f"creating IPv{version} subnet {network} (gateway {gateway}, {dhcp_range(network)})")
        pvesh(
            "create",
            path,
            subnet=str(network),
            type="subnet",
            gateway=str(gateway),
            dhcp_range=[dhcp_range(network)],
            dhcp_dns_server=str(dns) if dns else None,
        )
        result[version] = network
    return result[4], result[6]


def has_pending_sdn_changes() -> bool:
    """True if any zone, vnet or subnet differs from the applied config."""
    for entry in pvesh("get", "/cluster/sdn/zones", pending=1) or []:
        if entry.get("state"):
            return True
    for vnet in pvesh("get", "/cluster/sdn/vnets", pending=1) or []:
        if vnet.get("state"):
            return True
        for entry in pvesh("get", f"/cluster/sdn/vnets/{vnet['vnet']}/subnets", pending=1) or []:
            if entry.get("state"):
                return True
    return False


def apply_sdn() -> None:
    """Apply the SDN configuration cluster-wide and wait for the task."""
    log("applying SDN configuration (reloads networking on every node)")
    upid = pvesh("set", "/cluster/sdn")
    if not isinstance(upid, str) or not upid.startswith("UPID:"):
        return
    node = upid.split(":")[1]
    deadline = time.monotonic() + SDN_APPLY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        status = pvesh("get", f"/nodes/{node}/tasks/{upid}/status")
        if status.get("status") == "stopped":
            exit_status = status.get("exitstatus", "")
            if exit_status == "OK" or exit_status.startswith("WARNINGS"):
                log(f"SDN apply finished: {exit_status}")
                return
            lines = pvesh("get", f"/nodes/{node}/tasks/{upid}/log", limit=200) or []
            details = "\n".join(line.get("t", "") for line in lines)
            raise DeployError(f"SDN apply failed: {exit_status}\n{details}")
        time.sleep(2)
    raise DeployError(f"SDN apply did not finish within {SDN_APPLY_TIMEOUT_SECONDS}s ({upid})")


# --------------------------------------------------------------------------
# Node setup
# --------------------------------------------------------------------------

# Installs dnsmasq for the SDN DHCP backend without ever starting its default
# instance: a temporary policy-rc.d keeps apt from starting a resolver on all
# interfaces, and the default unit is then disabled as the Proxmox docs require
# (SDN runs its own dnsmasq@<zone> instances).
DNSMASQ_SCRIPT = r"""
set -euo pipefail
if dpkg-query -W -f='${Status}' dnsmasq 2>/dev/null | grep -q 'install ok installed'; then
    echo "dnsmasq already installed"
    if systemctl is-enabled --quiet dnsmasq 2>/dev/null; then
        echo "WARNING: the default dnsmasq instance is enabled; Proxmox SDN expects it disabled"
    fi
    exit 0
fi
created_policy=0
if [ ! -e /usr/sbin/policy-rc.d ]; then
    printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
    chmod 755 /usr/sbin/policy-rc.d
    created_policy=1
fi
cleanup() { if [ "$created_policy" = 1 ]; then rm -f /usr/sbin/policy-rc.d; fi; }
trap cleanup EXIT
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq dnsmasq
systemctl disable --now dnsmasq
echo "dnsmasq installed, default instance disabled"
"""


def install_agent_script() -> str:
    """Build a self-contained script that installs and (re)starts the agent."""
    agent = base64.b64encode(AGENT_SOURCE.read_bytes()).decode()
    unit = base64.b64encode(UNIT_SOURCE.read_bytes()).decode()
    return f"""
set -euo pipefail
echo '{agent}' | base64 -d > {AGENT_TARGET}.new
chmod 755 {AGENT_TARGET}.new
mv {AGENT_TARGET}.new {AGENT_TARGET}
echo '{unit}' | base64 -d > {UNIT_TARGET}
chmod 644 {UNIT_TARGET}
systemctl daemon-reload
systemctl enable --quiet {SERVICE_NAME}
systemctl restart {SERVICE_NAME}
sleep 2
systemctl is-active {SERVICE_NAME}
"""


UNINSTALL_AGENT_SCRIPT = f"""
set -uo pipefail
systemctl disable --now {SERVICE_NAME} 2>/dev/null
if [ -x {AGENT_TARGET} ]; then {AGENT_TARGET} cleanup; fi
rm -f {AGENT_TARGET} {UNIT_TARGET}
systemctl daemon-reload
echo "agent removed"
"""


def write_shared_config(settings: Settings, subnet4: IPNetwork, subnet6: IPNetwork) -> None:
    """Record the resolved settings in pmxcfs, where every node's agent reads them."""
    extra = " ".join(str(network) for network in settings.extra_blocked)
    timestamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    content = f"""\
# Generated by pve-tools isolated-net deploy.py on {timestamp}.
# Shared by all nodes and read by isolated-net-agent; re-run deploy.py
# instead of editing it. SUBNET4/SUBNET6 record the resolved subnets: copy
# them into isolated-net.conf to reproduce the exact same network elsewhere.
ZONE={settings.zone}
VNET={settings.vnet}
SUBNET4={subnet4}
SUBNET6={subnet6}
SITE_PREFIX_LEN6={settings.site_prefix_len6}
EXTRA_BLOCKED="{extra}"
"""
    if SHARED_CONFIG_PATH.exists() and SHARED_CONFIG_PATH.read_text().split("\n", 1)[1:] == content.split("\n", 1)[1:]:
        return
    SHARED_CONFIG_PATH.write_text(content)
    log(f"wrote {SHARED_CONFIG_PATH}")


def guests_using_vnet(vnet: str) -> list[str]:
    """Return 'node/type/vmid' of every guest with a NIC on the vnet."""
    pattern = re.compile(rf"^net\d+:.*\bbridge={re.escape(vnet)}(,|$)", re.MULTILINE)
    users = []
    for config in sorted(Path("/etc/pve/nodes").glob("*/*/*.conf")):
        if config.parent.name in ("qemu-server", "lxc") and pattern.search(config.read_text()):
            users.append(f"{config.parent.parent.name}/{config.parent.name}/{config.stem}")
    return users


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def require_root_on_pve() -> None:
    if os.geteuid() != 0:
        raise DeployError("run as root on a Proxmox VE node")
    if not Path("/usr/bin/pvesh").exists():
        raise DeployError("pvesh not found: this is not a Proxmox VE node")


def cmd_install(args: argparse.Namespace) -> None:
    settings = Settings.load(args.config)
    nodes = cluster_nodes()
    log(f"nodes: {', '.join(node.name for node in nodes)}")

    for node in nodes:
        log(f"[{node.name}] ensuring dnsmasq")
        print(node.run_script(DNSMASQ_SCRIPT).strip())

    ensure_zone(settings)
    ensure_vnet(settings)
    subnet4, subnet6 = ensure_subnets(settings, nodes)
    write_shared_config(settings, subnet4, subnet6)

    for node in nodes:
        log(f"[{node.name}] installing {SERVICE_NAME}")
        print(node.run_script(install_agent_script()).strip())

    if args.skip_apply:
        log("skipping SDN apply as requested (run 'pvesh set /cluster/sdn' later)")
    elif has_pending_sdn_changes() or args.force_apply:
        apply_sdn()
    else:
        log("SDN configuration already applied")

    for node in nodes:
        node.run_script(f"systemctl reload {SERVICE_NAME}")

    print()
    print(f"Isolated network ready: attach guests to bridge={settings.vnet}")
    print(f"  IPv4 {subnet4}  gateway {subnet4.network_address + 1}")
    print(f"  IPv6 {subnet6}  gateway {subnet6.network_address + 1}")
    print("  VMs: DHCP on both families.  LXC: ip=dhcp,ip6=dhcp.")


def cmd_status(_args: argparse.Namespace) -> None:
    if not SHARED_CONFIG_PATH.exists():
        raise DeployError(f"{SHARED_CONFIG_PATH} not found: not deployed yet")
    config = parse_key_value_file(SHARED_CONFIG_PATH)
    zone, vnet = config.get("ZONE", ""), config.get("VNET", "")
    print(f"zone {zone}, vnet {vnet}, IPv4 {config.get('SUBNET4')}, IPv6 {config.get('SUBNET6')}")
    print(f"pending SDN changes: {'yes' if has_pending_sdn_changes() else 'no'}")

    for node in cluster_nodes():
        state = node.run_script(
            f"systemctl is-active {SERVICE_NAME} || true; systemctl is-active dnsmasq@{zone} || true"
        ).split()
        print(f"[{node.name}] agent: {state[0]}, dhcp: {state[1] if len(state) > 1 else 'unknown'}")

    print("\nIPAM allocations:")
    entries = [e for e in pvesh("get", "/cluster/sdn/ipams/pve/status") or [] if e.get("zone") == zone]
    for entry in sorted(entries, key=lambda e: (str(e.get("vmid", "")), e.get("ip", ""))):
        owner = "gateway" if entry.get("gateway") else f"vmid {entry.get('vmid', '?')}"
        print(f"  {entry.get('ip', ''):<40} {entry.get('mac', '') or '':<18} {owner} {entry.get('hostname', '') or ''}")


def cmd_uninstall(args: argparse.Namespace) -> None:
    nodes = cluster_nodes()
    for node in nodes:
        log(f"[{node.name}] removing {SERVICE_NAME}")
        print(node.run_script(UNINSTALL_AGENT_SCRIPT).strip())

    if not args.purge:
        log("agent removed; SDN zone/vnet kept (use --purge to delete them too)")
        return

    config = parse_key_value_file(SHARED_CONFIG_PATH) if SHARED_CONFIG_PATH.exists() else {}
    settings = Settings.load(args.config)
    zone, vnet = config.get("ZONE", settings.zone), config.get("VNET", settings.vnet)
    users = guests_using_vnet(vnet)
    if users:
        raise DeployError(f"vnet {vnet} is still used by: {', '.join(users)}")

    vnets = {entry["vnet"] for entry in pvesh("get", "/cluster/sdn/vnets") or []}
    if vnet in vnets:
        for entry in pvesh("get", f"/cluster/sdn/vnets/{vnet}/subnets") or []:
            log(f"deleting subnet {entry['subnet']}")
            pvesh("delete", f"/cluster/sdn/vnets/{vnet}/subnets/{entry['subnet']}")
        log(f"deleting vnet {vnet}")
        pvesh("delete", f"/cluster/sdn/vnets/{vnet}")
    if zone in {entry["zone"] for entry in pvesh("get", "/cluster/sdn/zones") or []}:
        log(f"deleting zone {zone}")
        pvesh("delete", f"/cluster/sdn/zones/{zone}")
    apply_sdn()
    if SHARED_CONFIG_PATH.exists():
        SHARED_CONFIG_PATH.unlink()
    log("isolated network removed")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy an isolated NAT network on Proxmox VE SDN.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="settings file")
    commands = parser.add_subparsers(dest="command", required=True)

    install = commands.add_parser("install", help="create or reconcile the network (idempotent)")
    install.add_argument("--skip-apply", action="store_true", help="do not apply the SDN configuration")
    install.add_argument("--force-apply", action="store_true", help="apply even if nothing is pending")
    install.set_defaults(handler=cmd_install)

    status = commands.add_parser("status", help="show the network, agents and IPAM allocations")
    status.set_defaults(handler=cmd_status)

    uninstall = commands.add_parser("uninstall", help="remove the agent (and with --purge the SDN objects)")
    uninstall.add_argument("--purge", action="store_true", help="also delete subnets, vnet and zone")
    uninstall.set_defaults(handler=cmd_uninstall)

    args = parser.parse_args(argv)
    try:
        require_root_on_pve()
        args.handler(args)
    except DeployError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
