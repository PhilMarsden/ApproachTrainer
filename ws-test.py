#!/usr/bin/env python3
"""
ws-test.py — quick smoke test for the MSFS Bridge relay WebSocket.

Checks (in order):
  1. TCP port reachable                     (does anything listen there?)
  2. WebSocket handshake completes          (is it actually a WS endpoint?)
  3. Stream messages and pretty-print them  (is data flowing?)

Usage
-----
    python ws-test.py                              # ws://127.0.0.1:9090
    python ws-test.py ws://127.0.0.1:9090
    python ws-test.py ws://192.168.1.20:9090       # PC's LAN IP from phone

Press Ctrl-C to stop.
"""
from __future__ import annotations
import asyncio
import json
import socket
import sys
import time
from urllib.parse import urlparse

DEFAULT_URL = "ws://127.0.0.1:9090"


def tcp_probe(host: str, port: int, timeout: float = 2.0):
    """Plain stdlib TCP connect — confirms something is listening on the port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, None
    except Exception as e:
        return False, e


def fmt(v, spec=""):
    """Format a value that might be None."""
    if v is None:
        return "—"
    try:
        return format(v, spec) if spec else str(v)
    except Exception:
        return str(v)


async def stream(url: str):
    try:
        import websockets
    except ImportError:
        print("ERROR: 'websockets' library not installed.")
        print("       Run:  pip install websockets")
        sys.exit(2)

    print(f"[2/2] WS handshake → {url}")
    try:
        async with websockets.connect(url, open_timeout=5) as ws:
            print("       CONNECTED. Streaming messages — Ctrl-C to stop.\n")
            print(f"  {'#':>4}  {'src':<6} {'lat':>11} {'lon':>11}  "
                  f"{'alt_ft':>7}  {'hdg':>5}  {'gs_kt':>5}")
            print("  " + "-" * 60)
            last = time.time()
            count = 0
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                except asyncio.TimeoutError:
                    gap = int(time.time() - last)
                    print(f"  … no message for {gap}s. "
                          f"Is MSFS Bridge actually broadcasting?")
                    last = time.time()
                    continue

                count += 1
                last = time.time()
                try:
                    d = json.loads(raw)
                    print(f"  {count:>4}  "
                          f"{fmt(d.get('src')):<6} "
                          f"{fmt(d.get('lat'),'11.6f')} "
                          f"{fmt(d.get('lon'),'11.6f')}  "
                          f"{fmt(d.get('alt_ft'),'7.0f')}  "
                          f"{fmt(d.get('hdg'),'5.1f')}  "
                          f"{fmt(d.get('gs_kt'),'5.1f')}")
                except json.JSONDecodeError:
                    print(f"  {count:>4}  raw: {raw!r}")
    except OSError as e:
        print(f"       FAILED: {e}")
        print("\n  Looks like the WebSocket handshake didn't complete.")
        print("  Is the port really the relay (and not some other service)?")
        sys.exit(3)
    except Exception as e:
        # websockets-specific exceptions surface here
        print(f"       FAILED: {type(e).__name__}: {e}")
        sys.exit(3)


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    if "://" not in url:
        url = "ws://" + url
    p = urlparse(url)
    host = p.hostname or "127.0.0.1"
    port = p.port or 9090

    print(f"Target: {url}    (host={host}, port={port})\n")

    ok, err = tcp_probe(host, port)
    if ok:
        print(f"[1/2] TCP connect to {host}:{port}  OK")
    else:
        print(f"[1/2] TCP connect to {host}:{port}  FAILED: {err}")
        print()
        print("  Likely causes:")
        print("   • Relay not running.  Start it:  python msfs-bridge-relay.py")
        print("   • Wrong host/port.")
        print("   • Firewall blocking inbound on this port.")
        print("   • Relay running on a different machine — use that PC's LAN IP.")
        sys.exit(1)
    print()

    try:
        asyncio.run(stream(url))
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
