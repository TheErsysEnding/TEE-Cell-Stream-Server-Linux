"""Where the discovery beacon goes (port of Server.GetBeaconTargets).

The machine can have several network adapters (libvirt and VirtualBox add virtual ones), and a plain
255.255.255.255 broadcast only leaves through ONE of them - often the wrong one. So the beacon goes to
every adapter's own broadcast address (e.g. 10.42.0.255) plus the global one.
"""

import ipaddress
import json
import shutil
import subprocess

from . import log, protocol

GLOBAL_BROADCAST = "255.255.255.255"
IP_ARGUMENTS = ["-j", "-4", "addr", "show", "up"]
IP_TIMEOUT_S = 5

_failure_reported = False   # a missing/broken `ip` is worth one line, not one every 30 s refresh


def parse_beacon_targets(ip_json: str, port: int = protocol.BEACON_PORT) -> list[tuple[str, int]]:
    """Beacon targets from the JSON of `ip -j -4 addr show up`. Never raises: garbage gives just the global one."""
    targets = [(GLOBAL_BROADCAST, port)]
    try:
        interfaces = json.loads(ip_json)
    except ValueError:
        return targets
    if not isinstance(interfaces, list):
        return targets

    for interface in interfaces:
        if not isinstance(interface, dict):
            continue
        flags = interface.get("flags") or []
        # the original's filters: operationally up, and not the loopback. "UP" alone is only the admin
        # flag - a port without a cable (or libvirt's idle virbr0) is UP with NO-CARRIER / operstate DOWN
        # and gets nothing. operstate UNKNOWN is what working bridges and tunnels report, so it passes.
        if "LOOPBACK" in flags or "UP" not in flags:
            continue
        if "NO-CARRIER" in flags or interface.get("operstate") == "DOWN":
            continue
        for address in interface.get("addr_info") or []:
            if not isinstance(address, dict) or address.get("family") != "inet":
                continue
            broadcast = _broadcast_of(address, flags)
            if broadcast is None:
                continue
            target = (broadcast, port)
            if target not in targets:
                targets.append(target)
    return targets


def _broadcast_of(address: dict, flags: list) -> str | None:
    """The broadcast address `ip` printed, or (like the original: ip | ~mask) derived from the prefix."""
    broadcast = address.get("broadcast")
    if not broadcast:
        # `ip` only prints one where the link has one; point-to-point links (VPN tunnels) have none
        if "BROADCAST" not in flags or "POINTOPOINT" in flags:
            return None
        try:
            network = ipaddress.IPv4Interface((address.get("local"), address.get("prefixlen", 32))).network
        except (ValueError, TypeError):
            return None
        broadcast = str(network.broadcast_address)
    try:
        ipaddress.IPv4Address(broadcast)
    except ValueError:
        return None
    return broadcast


def get_beacon_targets() -> list[tuple[str, int]]:
    """The global broadcast plus every live interface's own; the global one alone if `ip` cannot be asked."""
    global _failure_reported
    # a desktop autostart may run with a PATH without /usr/sbin, where iproute2 lives
    ip_command = shutil.which("ip") or "/usr/sbin/ip"
    try:
        result = subprocess.run([ip_command] + IP_ARGUMENTS, capture_output=True, text=True,
                                timeout=IP_TIMEOUT_S, check=False)
        if result.returncode != 0:
            raise RuntimeError("Exit %d: %s" % (result.returncode, result.stderr.strip()[:120]))
        output = result.stdout
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        if not _failure_reported:
            _failure_reported = True
            log.write("beacon: `ip addr` fehlgeschlagen (%s), sende nur an %s" % (error, GLOBAL_BROADCAST))
        return [(GLOBAL_BROADCAST, protocol.BEACON_PORT)]
    return parse_beacon_targets(output)
