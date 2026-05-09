#!/usr/bin/env python3
"""
msfs-bridge-relay.py
====================

Tiny relay for the Instrument Approach Simulator: it listens for position
broadcasts from MSFS Bridge (or any GDL90 / NMEA / XGPS source) on UDP and
re-publishes them as JSON messages over a local WebSocket so that the
browser-based simulator can consume them.

Setup
-----
    pip install websockets

Run
---
    python msfs-bridge-relay.py
    # defaults: UDP listen port 49002, WS listen port 9090

Then in MSFS Bridge, configure it to broadcast to this machine's address on
UDP/49002 (the ForeFlight default that MSFS Bridge uses by default), and in
the simulator pick "MSFS Bridge" with URL ws://<this-host>:9090

Order
-----
The bridge can be started before or after the relay - UDP is connectionless,
so packets arriving while the relay is down are simply dropped by the OS, and
the relay starts catching them as soon as it binds the port.

Supported wire formats
----------------------
  * ForeFlight XGPS / XATT  (text, default on UDP 49002 - what MSFS Bridge sends)
  * GDL90 Ownship Report    (binary, framed with 0x7E flags - typical UDP 4000)
  * NMEA $GPGGA / $GPRMC    (text, fallback)

CLI
---
    --udp-port    UDP port to listen on (default 49002)
    --ws-host     WebSocket bind host    (default 0.0.0.0 - LAN-reachable)
    --ws-port     WebSocket port         (default 9090)
    --quiet       Suppress per-packet logging (heartbeat only)
    --verbose     Extra detail per packet (full hex + ASCII preview)

Output JSON shape (one message per parsed position fix):
    {"lat": <deg>, "lon": <deg>, "alt_ft": <ft MSL or null>,
     "hdg": <deg or null>, "gs_kt": <knots or null>,
     "src": "xgps"|"gdl90"|"nmea", "ts": <unix seconds>}
"""

from __future__ import annotations
import argparse
import asyncio
import json
import socket
import time
from typing import Optional

# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------
# Bump this on every meaningful change. Printed at startup so it's obvious
# at a glance which copy is actually running.
#
#   0.4.0  Add __version__ + --version flag
#   0.3.1  Strip NUL bytes from text datagrams (MSFS Bridge null-terminates)
#   0.3.0  ForeFlight XGPS/XATT parser, default UDP port -> 49002
#   0.2.0  Per-packet logging, heartbeat, port-in-use error reporting
#   0.1.0  Initial relay (GDL90 + NMEA on UDP 4000)
__version__ = "0.4.0"

try:
    import websockets
except ImportError:
    raise SystemExit("Missing dependency. Run:  pip install websockets")


M_TO_FT = 3.28083989501312
MPS_TO_KT = 1.9438444924406046

# Module-level shared state for XATT heading (XATT comes in separate datagrams
# from XGPS; we merge the most-recent XATT heading into the next XGPS fix).
_last_xatt = {"hdg": None, "pitch": None, "roll": None, "ts": 0.0}


# ---------------------------------------------------------------------------
# ForeFlight broadcast (XGPS / XATT) - text CSV after a 4-char tag
# ---------------------------------------------------------------------------
#
# XGPS<sim>,<lon>,<lat>,<alt_m_msl>,<track_deg>,<gs_m/s>
# XATT<sim>,<heading_true>,<pitch>,<roll>
#
# Example as MSFS Bridge sends them (one per UDP datagram, multiple per second):
#   XGPSMSFS,-122.4194,37.7749,1500.0,89.5,30.5
#   XATTMSFS,89.5,2.1,-1.2
#
def parse_foreflight(text: str) -> Optional[dict]:
    """Parse a single XGPS or XATT line.

    Returns a position dict for XGPS (with lat/lon/alt/hdg/gs).
    For XATT, updates module state and returns a small attitude dict
    (no lat/lon, so the browser side will skip it but the relay logs it
    as a recognised XATT packet rather than UNPARSED).
    """
    if not text:
        return None
    line = text.strip().split("\n", 1)[0].strip()
    if not line.startswith("X"):
        return None
    parts = line.split(",")
    if not parts:
        return None
    tag = parts[0]

    if tag.startswith("XGPS") and len(parts) >= 6:
        try:
            lon = float(parts[1])
            lat = float(parts[2])
            alt_m = float(parts[3]) if parts[3] != "" else None
            track = float(parts[4]) if parts[4] != "" else None
            gs_mps = float(parts[5]) if parts[5] != "" else None
        except ValueError:
            return None

        # Prefer XATT heading if it's fresh (last 2 s); else fall back to XGPS track.
        hdg: Optional[float]
        if (_last_xatt["hdg"] is not None
                and (time.time() - _last_xatt["ts"]) < 2.0):
            hdg = _last_xatt["hdg"]
        else:
            hdg = track

        return {
            "lat": lat,
            "lon": lon,
            "alt_ft": (alt_m * M_TO_FT) if alt_m is not None else None,
            "hdg": hdg,
            "gs_kt": (gs_mps * MPS_TO_KT) if gs_mps is not None else None,
            "src": "xgps",
            "ts": time.time(),
        }

    if tag.startswith("XATT") and len(parts) >= 4:
        try:
            heading = float(parts[1])
            pitch = float(parts[2]) if parts[2] != "" else None
            roll = float(parts[3]) if parts[3] != "" else None
        except ValueError:
            return None
        _last_xatt["hdg"] = heading
        _last_xatt["pitch"] = pitch
        _last_xatt["roll"] = roll
        _last_xatt["ts"] = time.time()
        # Return an attitude-only dict (no lat/lon) so the relay logs it
        # as recognised; the browser will skip it because it has no lat/lon.
        return {
            "src": "xatt",
            "hdg": heading,
            "pitch": pitch,
            "roll": roll,
            "ts": time.time(),
        }

    return None


# ---------------------------------------------------------------------------
# GDL90 frame parsing
# ---------------------------------------------------------------------------

FLAG = 0x7E
ESC = 0x7D


def gdl90_unframe(buf: bytes) -> list:
    """Split a UDP datagram into GDL90 frames, removing flags and byte-stuffing."""
    frames = []
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
    return frames


def parse_gdl90_ownship(p: bytes) -> Optional[dict]:
    """Parse a GDL90 Ownship Report (Message ID 10 / 0x0A)."""
    if len(p) < 28 or p[0] != 0x0A:
        return None

    def s24(b0: int, b1: int, b2: int) -> int:
        v = (b0 << 16) | (b1 << 8) | b2
        if v & 0x800000:
            v -= 0x1000000
        return v

    lat = s24(p[5], p[6], p[7]) * (180.0 / (1 << 23))
    lon = s24(p[8], p[9], p[10]) * (180.0 / (1 << 23))
    alt_raw = (p[11] << 4) | (p[12] >> 4)
    alt_ft = None if alt_raw == 0xFFF else (alt_raw * 25.0 - 1000.0)
    hv_raw = (p[13] << 4) | (p[14] >> 4)
    gs_kt = None if hv_raw == 0xFFF else float(hv_raw)
    hdg = (p[16] * (360.0 / 256.0)) if len(p) > 16 else None
    return {
        "lat": lat, "lon": lon,
        "alt_ft": alt_ft,
        "hdg": hdg, "gs_kt": gs_kt,
        "src": "gdl90", "ts": time.time(),
    }


# ---------------------------------------------------------------------------
# NMEA fallback
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
            "alt_ft": (alt_m * M_TO_FT) if alt_m is not None else None,
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


# ---------------------------------------------------------------------------
# Datagram entry point: try formats in order
# ---------------------------------------------------------------------------

def parse_datagram(data: bytes) -> Optional[dict]:
    """Try ForeFlight XGPS/XATT, then GDL90, then NMEA."""
    if not data:
        return None

    # 1. ForeFlight XGPS / XATT (text, starts with 'X')
    if data[:1] == b"X":
        try:
            # MSFS Bridge null-terminates each datagram. Python's float()
            # ignores whitespace but NOT NUL bytes, so we strip them here
            # otherwise the last numeric field fails to parse.
            text = data.decode("ascii", errors="ignore").replace("\x00", "")
        except Exception:
            text = ""
        if text:
            # A datagram may contain multiple lines (e.g. XGPS + XATT). Parse
            # each; keep the most recent positional fix to forward.
            latest = None
            for line in text.splitlines():
                m = parse_foreflight(line)
                if m:
                    latest = m  # last wins; XATT-only updates state
            if latest:
                return latest

    # 2. GDL90 (binary, framed with 0x7E)
    if data[:1] == b"\x7e" or b"\x7e" in data[:2]:
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

    # 3. NMEA (text $GP...)
    try:
        text = data.decode("ascii", errors="ignore").replace("\x00", "")
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
        self.clients = set()
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
    peer = getattr(ws, "remote_address", None)
    print(f"  [ws] client connected: {peer}")
    await hub.register(ws)
    try:
        async for _ in ws:
            pass
    finally:
        await hub.unregister(ws)
        print(f"  [ws] client disconnected: {peer}")


# ---------------------------------------------------------------------------
# UDP listener
# ---------------------------------------------------------------------------

class UDPProto(asyncio.DatagramProtocol):
    """UDP listener: logs every datagram, parses, fans out via Hub."""

    def __init__(self, hub, loop, verbose: bool, quiet: bool):
        self.hub = hub
        self.loop = loop
        self.verbose = verbose
        self.quiet = quiet
        self.total_rx = 0
        self.bytes_rx = 0
        self.parsed = 0
        self.unparsed = 0
        self.attitude_only = 0
        self.last_msg = None
        self.last_addr = None
        self.sources = {}

    def datagram_received(self, data: bytes, addr) -> None:
        self.total_rx += 1
        self.bytes_rx += len(data)
        self.last_addr = addr
        self.sources[addr] = self.sources.get(addr, 0) + 1

        m = parse_datagram(data)

        # 1. Position fix (lat + lon present) - log + broadcast
        if m and "lat" in m and "lon" in m:
            self.parsed += 1
            self.last_msg = m
            payload = json.dumps(m, separators=(",", ":"))
            asyncio.ensure_future(self.hub.broadcast(payload))

            if not self.quiet:
                src_lbl = m["src"]
                alt = m["alt_ft"]
                alt_s = f"{alt:>5.0f} ft" if alt is not None else "  -- ft"
                hdg = m["hdg"]
                hdg_s = f"{hdg:5.1f}d" if hdg is not None else "  --d"
                gs = m["gs_kt"]
                gs_s = f"{gs:4.0f} kt" if gs is not None else "  -- kt"
                print(f"  RX {len(data):>4}B  {addr[0]}:{addr[1]:<5}  "
                      f"[{src_lbl:<5}]  "
                      f"{m['lat']:>10.6f}, {m['lon']:>11.6f}   "
                      f"{alt_s}  hdg={hdg_s}  gs={gs_s}")
                if self.verbose:
                    print(f"          hex: {data[:32].hex(' ')}"
                          + ("  ..." if len(data) > 32 else ""))
            return

        # 2. Recognised non-position packet (XATT) - log briefly, don't broadcast
        if m and m.get("src") == "xatt":
            self.attitude_only += 1
            if not self.quiet:
                hdg = m.get("hdg")
                pitch = m.get("pitch")
                roll = m.get("roll")
                hdg_s = f"{hdg:5.1f}d" if hdg is not None else "  --d"
                pitch_s = f"{pitch:5.1f}d" if pitch is not None else "  --d"
                roll_s = f"{roll:5.1f}d" if roll is not None else "  --d"
                print(f"  RX {len(data):>4}B  {addr[0]}:{addr[1]:<5}  "
                      f"[xatt ]  hdg={hdg_s}  pitch={pitch_s}  roll={roll_s}")
            return

        # 3. Unparsed - this is the diagnostic case
        self.unparsed += 1
        head_hex = data[:32].hex(" ")
        ellip = "  ..." if len(data) > 32 else ""
        print(f"  RX {len(data):>4}B  {addr[0]}:{addr[1]:<5}  "
              f"[UNPARSED]  hex: {head_hex}{ellip}")
        if self.verbose:
            try:
                txt = data[:80].decode("ascii", errors="replace")
                txt = txt.replace("\r", "\\r").replace("\n", "\\n")
                print(f"          ascii: {txt!r}")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def heartbeat(proto: UDPProto, hub: Hub) -> None:
    """Print a periodic stats line so the user knows the relay is alive."""
    last_total = 0
    last_t = time.monotonic()
    while True:
        await asyncio.sleep(5.0)
        now = time.monotonic()
        dt = max(0.001, now - last_t)
        rate = (proto.total_rx - last_total) / dt
        last_total = proto.total_rx
        last_t = now

        if proto.total_rx == 0:
            print(f"  [heartbeat] no UDP packets received yet "
                  f"-- is MSFS Bridge configured to broadcast to this machine?  "
                  f"clients={len(hub.clients)}")
            continue

        srcs = ", ".join(
            f"{ip}:{p}x{n}"
            for (ip, p), n in sorted(proto.sources.items(), key=lambda kv: -kv[1])[:3]
        )
        last_fix = "--"
        if proto.last_msg:
            lm = proto.last_msg
            if lm.get("alt_ft") is not None:
                last_fix = f"{lm['lat']:.5f},{lm['lon']:.5f} alt={lm['alt_ft']:.0f} ft"
            else:
                last_fix = f"{lm['lat']:.5f},{lm['lon']:.5f}"
        print(f"  [heartbeat] rx={proto.total_rx} parsed={proto.parsed} "
              f"xatt={proto.attitude_only} unparsed={proto.unparsed} "
              f"rate={rate:.1f}/s ws_clients={len(hub.clients)}  "
              f"senders=[{srcs}]  last_fix={last_fix}")


async def main_async(args) -> None:
    hub = Hub()
    loop = asyncio.get_running_loop()

    proto_factory_holder = {"proto": None}

    def factory():
        p = UDPProto(hub, loop, args.verbose, args.quiet)
        proto_factory_holder["proto"] = p
        return p

    try:
        transport, _ = await loop.create_datagram_endpoint(
            factory,
            local_addr=("0.0.0.0", args.udp_port),
            family=socket.AF_INET,
            allow_broadcast=True,
        )
    except OSError as e:
        print(f"\nERROR: cannot bind UDP {args.udp_port}: {e}")
        if getattr(e, "errno", None) in (48, 98, 10048):
            print(f"       Port {args.udp_port} is already in use by another process.")
            print(f"       Stop whatever's holding it (an old relay? another bridge")
            print(f"       tool?) or run with --udp-port <other>.")
        return

    proto = proto_factory_holder["proto"]
    print(f"msfs-bridge-relay  v{__version__}")
    print(f"UDP listening on 0.0.0.0:{args.udp_port}  (XGPS/XATT, GDL90, NMEA)")
    print("  -> Configure MSFS Bridge to broadcast to this PC's IP on that port.")
    print("  -> Bridge can be started before or after this relay.")

    try:
        ws_server = await websockets.serve(
            lambda ws: ws_handler(ws, hub),
            args.ws_host, args.ws_port,
            ping_interval=20, ping_timeout=20,
        )
    except OSError as e:
        print(f"\nERROR: cannot bind WebSocket {args.ws_host}:{args.ws_port}: {e}")
        transport.close()
        return

    print(f"WebSocket on ws://{args.ws_host}:{args.ws_port}")
    print("  -> In the simulator pick 'MSFS Bridge' with that URL.")
    print()
    print("Logging every received UDP datagram below. Heartbeat every 5 s.")
    print("-" * 78)

    hb_task = asyncio.create_task(heartbeat(proto, hub))
    try:
        await asyncio.Future()
    finally:
        hb_task.cancel()
        ws_server.close()
        await ws_server.wait_closed()
        transport.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="MSFS Bridge UDP->WebSocket relay")
    ap.add_argument("--version", action="version",
                    version=f"msfs-bridge-relay {__version__}")
    ap.add_argument("--udp-port", type=int, default=49002,
                    help="UDP port to listen on (default 49002)")
    ap.add_argument("--ws-host", default="0.0.0.0",
                    help="WebSocket bind host (default 0.0.0.0)")
    ap.add_argument("--ws-port", type=int, default=9090,
                    help="WebSocket port (default 9090)")
    ap.add_argument("--quiet", action="store_true",
                    help="Suppress per-packet logging (heartbeat only)")
    ap.add_argument("--verbose", action="store_true",
                    help="Extra detail per packet (full hex + ASCII preview)")
    args = ap.parse_args()

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
