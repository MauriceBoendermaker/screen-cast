"""Finding Cast devices on the local network."""

from __future__ import annotations

import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor

import pychromecast


CAST_PORT = 8009
DISCOVERY_TIMEOUT = 8
PROBE_TIMEOUT = 0.6
PROBE_WORKERS = 128


def get_local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    finally:
        sock.close()


def probe_cast_port(host: str) -> str | None:
    sock = socket.socket()
    sock.settimeout(PROBE_TIMEOUT)

    try:
        if sock.connect_ex((host, CAST_PORT)) == 0:
            return host

        return None
    except OSError:
        return None
    finally:
        sock.close()


def scan_for_cast_hosts(local_ip: str) -> list[str]:
    """Find Cast devices by probing the local /24 directly.

    mDNS discovery finds nothing on networks where the router does not
    forward multicast between clients, so fall back to port 8009.
    """
    network = ipaddress.ip_network(f"{local_ip}/24", strict=False)
    hosts = [str(host) for host in network.hosts()]

    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        return [host for host in pool.map(probe_cast_port, hosts) if host]


def discover(
    known_hosts: list[str] | None,
) -> tuple[list[pychromecast.Chromecast], object]:
    return pychromecast.get_chromecasts(
        timeout=DISCOVERY_TIMEOUT,
        known_hosts=known_hosts,
    )


def device_label(cast: pychromecast.Chromecast) -> str:
    return cast.cast_info.friendly_name or str(cast.cast_info.host)


def sort_devices(
    chromecasts: list[pychromecast.Chromecast],
) -> list[pychromecast.Chromecast]:
    return sorted(chromecasts, key=lambda cast: device_label(cast).lower())


def select_by_name(
    chromecasts: list[pychromecast.Chromecast],
    name: str,
) -> pychromecast.Chromecast:
    for cast in chromecasts:
        if device_label(cast).lower() == name.lower():
            return cast

    available = ", ".join(repr(device_label(cast)) for cast in chromecasts)

    raise RuntimeError(f"No device named {name!r}. Found: {available}")


def discover_devices(
    host: str | None = None,
    on_status: object = None,
) -> tuple[list[pychromecast.Chromecast], object]:
    """Return every Cast device found, plus the browser keeping them alive.

    The caller owns the browser and must call stop_discovery() on it.
    """
    report = on_status if callable(on_status) else lambda message: None

    if host is not None:
        report(f"Looking for a Cast device at {host}...")

        chromecasts, browser = discover([host])

        if not chromecasts:
            browser.stop_discovery()
            raise RuntimeError(f"No Cast device responded at {host}.")

        return sort_devices(chromecasts), browser

    report("Searching for Cast devices...")

    chromecasts, browser = discover(None)

    if chromecasts:
        return sort_devices(chromecasts), browser

    browser.stop_discovery()

    report("No reply over mDNS. Scanning the local network...")

    known_hosts = scan_for_cast_hosts(get_local_ip())

    if not known_hosts:
        raise RuntimeError(
            "No Cast devices found. Check that the device is powered on "
            "and on the same network, then retry with --host <ip>."
        )

    chromecasts, browser = discover(known_hosts)

    if not chromecasts:
        browser.stop_discovery()

        found = ", ".join(known_hosts)

        raise RuntimeError(
            f"Hosts {found} listen on port {CAST_PORT} but did not "
            "identify themselves as Cast devices."
        )

    return sort_devices(chromecasts), browser
