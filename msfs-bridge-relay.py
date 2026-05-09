#!/usr/bin/env python3
"""
msfs-bridge-relay.py
====================

Tiny relay for the Instrument Approach Simulator: it listens for position
broadcasts from MSFS Bridge (or any GDL90 / NMEA source) on UDP and
re-publishes them as JSON messages over a local WebSocket so that the
browser-based simulator can consume them.

Setup
-----
    pip install websockets

Run
---
    python msfs-bridge-relay.py
    # defaults: UDP listen port 4000, WS listen port 9090

Then in MSFS Bridge, configure it to broadcast GDL90 (or NMEA) to this
machine's address on UDP/4000 (the typical ForeFlight default), and in
the simulator pick "MSFS Bridge" with URL ws://<this-host>:9090

CLI
---
    --udp-port    UDP port to listen on (default 4000)
    --ws-host     WebSocket bind host    (default 0.0.0.0 — LAN-reachable)
    --ws-port     WebSocket port         (default 9090)
    --verbose     Log every parsed packet

Output JSON shape (one message per parsed packet):
    {"lat": <deg>, "lon": <deg>, "alt_ft": <ft MSL or null>,
     "hdg": <deg or null>, "gs_kt": <knots or null>,
     "src": "gdl90"|"nmea", "ts": <unix seconds>}
"""

from __future__ import annotations
import argparse
import asyncio
import json
import socket
import time
from typing import Optional

try:
    import websockets
except ImportError:
    raise SystemExit("Missing dependency. Run:  pip install websockets")


# ---------------------------------------------------------------------------
# GDL90 frame parsing
# ---------------------------------------------------------------------------

FLAG = 0x7E
ESC  = 0x7D


def gdl90_unframe(buf: bytes) -> list[bytes]:
    """Split a UDP datagram into GDL90 frames, removing flags and byte-stuffing.

    Each datagram from MSFS Bridge / ForeFlight bridges normally contains a
    single framed message bracketed by 0x7E flags, but we handle multiple.
    The 16-bit FCS is stripped (we don't verify it; UDP already has a CRC).
    """
    frames: list[bytes] = []
    i, n = 0, len(buf)
    while i < n:
        if buf[i] != FLAG:
            i += 1
            continue
        i += 1
        out = bytearray()
        while i < n and buf[i] != FLAG:
            b = buf[i]
            if b == ESC and i + 1 < n:
                out.append(buf[i + 1] ^ 0x20)
                i += 2
            else:
                out.append(b)
                i += 1
        if i < n and len(out) > 2:
            frames.append(bytes(out[:-2]))  # strip 2-byte CRC
        # leave i pointing at the trailing flag — next loop will skip it
    return frames


def parse_gdl90_ownship(p: bytes) -> Optional[dict]:
    """Parse a GDL90 Ownship Report (Message ID 10 / 0x0A).

    Returns None if not an ownship message or the buffer is too short.
    Reference: GDL 90 Data Interface Specification, §3.4.
    """
    # Ownship message body is 28 bytes.
    if len(p) < 28 or p[0] != 0x0A:
        return None

    def s24(b0: int, b1: int, b2: int) -> int:
        v = (b0 << 16) | (b1 << 8) | b2
        if v & 0x800000:
            v -= 0x1000000
        return v

    # Latitude / longitude: 24-bit signed semicircles, LSB = 180 / 2^23 degrees
    lat = s24(p[5], p[6], p[7]) * (180.0 / (1 << 23))
    lon = s24(p[8], p[9], p[10]) * (180.0 / (1 << 23))

    # Pressure altitude: 12 bits, encoded as upper 12 bits of (p[11] p[12]).
    # Resolution = 25 ft, offset = -1000 ft, 0xFFF = invalid.
    alt_raw = (p[11] << 4) | (p[12] >> 4)
    alt_ft: Optional[float] = None if alt_raw == 0xFFF else (alt_raw * 25.0 - 1000.0)

    # Velocity / track sit at bytes 13–16 in compact form.
    # Horizontal velocity (kt): 12 bits at p[13] high8 + p[14] high4
    hv_raw = (p[13] << 4) | (p[14] >> 4)
    gs_kt: Optional[float] = None if hv_raw == 0xFFF else float(hv_raw)
    # Track / heading: 8 bits, byte 16, units = 360/256 deg
    hdg = (p[16] * (360.0 / 256.0)) if len(p) > 16 else None

    return {
        "lat": lat, "lon": lon,
        "alt_ft": alt_ft,
        "hdg": hdg, "gs_kt": gs_kt,
        "src": "gdl90", "ts": time.time(),
    }


# ---------------------------------------------------------------------------
# NMEA fallback (some bridges emit $GPGGA / $GPRMC instead of GDL90)
# ---------------------------------------------------------------------------

def _nmea_dm_to_deg(s: str, hemi: str) -> Optional[float]:
    if not s:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    deg = int(f // 100)
    minutes = f - deg * 100
    val = deg + minutes / 60.0
    if hemi in ("S", "W"):
        val = -val
    return val


def parse_nmea(line: str) -> Optional[dict]:
    line = line.strip()
    if not line.startswith("$") or "*" not in line:
        return None
    body = line.split("*", 1)[0]
    fields = body.split(",")
    sentence = fields[0][3:] if len(fields[0]) >= 5 else ""

    if sentence == "GGA" and len(fields) >= 10:
        lat = _nmea_dm_to_deg(fields[2], fields[3])
        lon = _nmea_dm_to_deg(fields[4], fields[5])
        try:
            alt_m = float(fields[9]) if fields[9] else None
        except ValueError:
            alt_m = None
        if lat is None or lon is None:
            return None
        return {
            "lat": lat, "lon": lon,
            "alt_ft": (alt_m * 3.28084) if alt_m is not None else None,
            "hdg": None, "gs_kt": None,
            "src": "nmea", "ts": time.time(),
        }

    if sentence == "RMC" and len(fields) >= 9:
        lat = _nmea_dm_to_deg(fields[3], fields[4])
        lon = _nmea_dm_to_deg(fields[5], fields[6])
        if lat is None or lon is None:
            return None
        try:
            gs_kt = float(fields[7]) if fields[7] else None
        except ValueError:
            gs_kt = None
        try:
            hdg = float(fields[8]) if fields[8] else None
        except ValueError:
            hdg = None
        return {
            "lat": lat, "lon": lon,
            "alt_ft": None,
            "hdg": hdg, "gs_kt": gs_kt,
            "src": "nmea", "ts": time.time(),
        }

    return None


def parse_datagram(data: bytes) -> Optional[dict]:
    """Try GDL90 first, then fall back to NMEA. Returns the most recent fix."""
    if not data:
        return None
    if data[:1] == b"\x7e" or b"\x7e" in data[:2]:
        # Looks framed — treat as GDL90.
        latest = None
        for f in gdl90_unframe(data):
            if not f:
                continue
            if f[0] == 0x0A:
                m = parse_gdl90_ownship(f)
                if m:
                    latest = m
        if latest:
            return latest
    # Try NMEA — datagram may be one or several lines
    try:
        text = data.decode("ascii", errors="ignore")
    except Exception:
        return None
    latest = None
    for line in text.splitlines():
        m = parse_nmea(line)
        if m:
            latest = m
    return latest


# ---------------------------------------------------------------------------
# WebSocket fanout
# ---------------------------------------------------------------------------

class Hub:
    def __init__(self) -> None:
        self.clients: set = set()
        self._lock = asyncio.Lock()

    async def register(self, ws):
        async with self._lock:
            self.clients.add(ws)

    async def unregister(self, ws):
        async with self._lock:
            self.clients.discard(ws)

    async def broadcast(self, payload: str):
        if not self.clients:
            return
        # Snapshot list to avoid mutation during iteration
        async with self._lock:
            targets = list(self.clients)
        await asyncio.gather(
            *[self._safe_send(c, payload) for c in targets],
            return_exceptions=True,
        )

    async def _safe_send(self, ws, payload):
        try:
            await ws.send(payload)
        except Exception:
            await self.unregister(ws)


async def ws_handler(ws, hub: Hub):
    await hub.register(ws)
    try:
        async for _ in ws:
            pass  # we ignore client messages; this is a one-way feed
    finally:
        await hub.unregister(ws)


# ---------------------------------------------------------------------------
# UDP listener (asyncio Datagram protocol)
# ---------------------------------------------------------------------------

class UDPProto(asyncio.DatagramProtocol):
    def __init__(self, hub: Hub, loop: asyncio.AbstractEventLoop, verbose: bool):
        self.hub = hub
        self.loop = loop
        self.verbose = verbose
        self.last_log = 0.0
        self.count = 0

    def datagram_received(self, data: bytes, addr) -> None:
        m = parse_datagram(data)
        if not m:
            return
        self.count += 1
        if self.verbose:
            print(f"[{m['src']}] {m['lat']:.6f},{m['lon']:.6f} "
                  f"alt={m['alt_ft']} hdg={m['hdg']} gs={m['gs_kt']}")
        else:
            now = time.monotonic()
            if now - self.last_log > 2.0:
                print(f"  fixes: {self.count}  last: {m['lat']:.5f},{m['lon']:.5f}  "
                      f"alt_ft={m['alt_ft']}")
                self.last_log = now
        payload = json.dumps(m, separators=(",", ":"))
        # Schedule the broadcast on the event loop (we're already on it).
        asyncio.ensure_future(self.hub.broadcast(payload))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main_async(args) -> None:
    hub = Hub()
    loop = asyncio.get_running_loop()

    # UDP listener
    transport, _ = await loop.create_datagram_endpoint(
        lambda: UDPProto(hub, loop, args.verbose),
        local_addr=("0.0.0.0", args.udp_port),
        family=socket.AF_INET,
        allow_broadcast=True,
    )
    print(f"UDP listening on 0.0.0.0:{args.udp_port}  (GDL90 / NMEA)")

    # WebSocket server
    async with websockets.serve(
        lambda ws: ws_handler(ws, hub),
        args.ws_host, args.ws_port,
        ping_interval=20, ping_timeout=20,
    ) as _server:
        print(f"WebSocket on ws://{args.ws_host}:{args.ws_port}")
        print("Connect the simulator and start MSFS Bridge.")
        try:
            await asyncio.Future()  # run forever
        finally:
            transport.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="MSFS Bridge UDP→WebSocket relay")
    ap.add_argument("--udp-port", type=int, default=4000,
                    help="UDP port to listen on (default 4000)")
    ap.add_argument("--ws-host", default="0.0.0.0",
                    help="WebSocket bind host (default 0.0.0.0)")
    ap.add_argument("--ws-port", type=int, default=9090,
                    help="WebSocket port (default 9090)")
    ap.add_argument("--verbose", action="store_true",
                    help="Log every parsed packet")
    args = ap.parse_args()

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
