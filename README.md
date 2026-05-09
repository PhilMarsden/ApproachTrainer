[README.md](https://github.com/user-attachments/files/27557600/README.md)
# Instrument Approach Simulator

A single-page web app that lets a pilot fly a simulated **ILS** or **RNP LNAV**
approach using either the device's real GPS, or position data from MSFS 2024
via MSFS Bridge.

> **Training simulation only. Not for navigation.**

The app maps your real-world position onto a virtual final-approach segment
and drives a Course Deviation Indicator (CDI). Walk around an open area to
fly the approach physically, or sit at home and fly it from the sim.

---

## Files

| File | What it is |
|---|---|
| `index.html` | The simulator. Open it in any modern browser. Self-contained. |
| `msfs-bridge-relay.py` | Optional Python relay that converts MSFS Bridge UDP packets into a WebSocket the simulator can read. Only needed for MSFS mode. |
| `ws-test.py` | Diagnostic — connects to the relay's WebSocket and prints incoming GPS messages, useful for verifying the relay is alive and forwarding correctly. |
| `parser_check.py` | Diagnostic — runs sample packets through the relay's parser to confirm the on-disk version is up to date. |

---

## Quick start

### Mode 1 — Flying with device GPS

1. Open `index.html` on a phone, tablet, or laptop with GPS.
2. Grant location permission when prompted.
3. Fill in the setup screen (see [Setup](#setup-screen) below).
4. Tap **Go**. Your current position becomes the simulated start; walk forward
   to fly inbound, sideways to drift off centerline.

### Mode 2 — At home with MSFS 2024 + MSFS Bridge

1. In MSFS Bridge, configure it to broadcast to your PC's address on
   **UDP port 49002** (the default — that's the ForeFlight broadcast port and
   the format MSFS Bridge speaks natively).
2. On the same PC, install the relay's one dependency and start the relay:

   ```bash
   pip install websockets
   python msfs-bridge-relay.py
   ```

   You should see lines like:

   ```
   msfs-bridge-relay  v0.4.0
   UDP listening on 0.0.0.0:49002  (XGPS/XATT, GDL90, NMEA)
   WebSocket on ws://0.0.0.0:9090
   ```

3. Open `index.html` in a browser on the same PC (or another device on the
   same LAN). On the setup screen, set **Location Source** to **MSFS Bridge**
   and enter the WebSocket URL:

   - Same machine: `ws://127.0.0.1:9090`
   - Different machine on LAN: `ws://<relay-PC-IP>:9090`
     (e.g. `ws://192.168.1.20:9090`)

4. Fly the sim. The simulator reads MSFS's lat/lon/altitude and drives the CDI
   exactly as if you were walking with real GPS.

The bridge can be started **before or after** the relay; UDP is connectionless
so the relay catches packets the moment it binds the port.

---

## Setup screen

| Field | What it means |
|---|---|
| **Location Source** | `Device GPS` for walking outdoors, `MSFS Bridge` for the sim. |
| **WebSocket URL** | Only shown for MSFS Bridge mode. Where to connect to the relay. Default `ws://127.0.0.1:9090`. |
| **Approach Type** | `ILS` (precision, full glideslope) or `RNP LNAV` (lateral with optional advisory VNAV). |
| **Distance from FAF (NM)** | How far ahead the simulated FAF is when you tap Go. Default 5.0. |
| **Platform Altitude (ft MSL)** | Altitude at the FAF — i.e., the altitude you're meant to be at when crossing the FAF inbound. Auto-fills with current GPS altitude on page load; type to override. |
| **Glideslope angle** | ILS only. Standard 3.0°. |
| **DA (ft MSL)** | ILS only. Decision altitude — the lowest you can descend on the GS without seeing the runway. Auto-defaults to *Field Elevation + 200 ft* (CAT I). |
| **Lateral only / Lat + Adv VNAV** | LNAV only. Whether to also show an advisory glide path. |
| **MDA / DA** | LNAV+V only. Minimum/decision altitude in ft MSL. |
| **MDA→Thr** | LNAV+V only. Distance from the MDA point to the threshold; defines the advisory descent angle. |
| **Approach course** | Magnetic, 1–360°. Leave blank to use the device compass at Go (or a heading derived after walking 10 m). |
| **Field elevation** | Airport surface elevation, in ft MSL. Used for path geometry. Default 0. |

After tapping **Go**, the app waits up to 10 seconds for the first position
fix. If GPS is denied or the bridge connection fails, you'll get a clear
error and a "Try again" button.

---

## Simulation screen

A black instrument panel with a CDI, six readouts, a vertical-mode toggle,
and three control buttons.

### CDI

A classic T-bar CDI rendered in SVG.

- **Vertical needle** — localizer (lateral). Indicates the **direction of the
  centerline relative to you**: if the needle is to the right, fly right to
  intercept; if it's to the left, fly left. Pegged at full-scale deflection
  with the needle stuck at the dot.
- **Horizontal needle** — glideslope (ILS or LNAV+V). Indicates **direction of
  the glide path**: needle UP means the path is above you (fly up); DOWN means
  you're above the path (fly down). Hidden for LNAV-only.
- **Two scale dots either side of centre** — half-scale and full-scale
  reference markers. ILS LOC ±2.5°, GS ±0.7°. RNP LNAV ±0.30 NM at distance,
  tightening to ±0.10 NM at the threshold.
- **TO/FROM triangle** — flips when you cross the FAF inbound vs outbound.
- **Mode label** (top-left of the bezel) — `ILS`, `LNAV`, or `LNAV+V`.
- **Version label** (bottom-left of the bezel) — small dim text confirming
  which version you're running.

### Readouts (six, in 3 rows × 2 columns)

| Readout | Meaning |
|---|---|
| **Dist FAF** | Along-track distance to the FAF. Shows "past FAF" once negative. |
| **Dist Thr** | Along-track distance to the threshold. |
| **Cur Alt** | Current altitude in ft MSL — value depends on the vertical mode (see below). |
| **Tgt Alt** | What altitude you should be at. Pre-FAF: platform altitude. Post-FAF for ILS: continuous GS-path altitude. Post-FAF for LNAV: snaps to the altitude at the next whole-NM crossing toward threshold (matches published step-down charts). Δ shows current minus target. |
| **Tgt FPM** | Target descent rate to maintain the GS at your current groundspeed. Computed as `gs_kt × (6076 / 60) × tan(angle)` — e.g., 90 kt on a 3° path ≈ 478 fpm. |
| **GS** | Groundspeed in knots. From the source's reported speed (MSFS Bridge `gs_kt` or device GPS `coords.speed`). |
| **Next Step** | Target altitude at the next whole-NM crossing toward threshold, plus which NM marker. |
| **Step Δ** | Current altitude minus that target. **Red "BELOW — climb"** if you're more than 10 ft below the minimum (dangerous). Amber "above" if higher. Green "on profile" if within 10 ft. |

### Mode and source labels (top status bar)

- **Mode tag** (left) — current approach: `ILS`, `LNAV`, or `LNAV+V`.
- **Course tag** (centre) — the inbound course in degrees magnetic.
- **GPS / BRIDGE tag** (right) — position source plus accuracy. Turns red if
  GPS accuracy is worse than 15 m, or shows `BRIDGE LOST` if the relay's
  WebSocket disconnects mid-flight.

### Vertical mode toggle (`Auto` / `GPS` / `Manual`)

Three ways to drive the *current* altitude (the value used for Cur Alt and
the GS-needle deviation):

- **Auto-on-path** *(default)* — Treats the pilot as exactly on the procedure
  altitude. Pre-FAF: holds platform altitude. Post-FAF: descends down the GS
  path. Useful for **pure lateral practice** — the GS needle stays centred
  during the descent so all you have to focus on is the localizer.
- **GPS** — Uses the *real* altitude from the position source. For walking
  with device GPS this is your physical altitude. For MSFS this is the sim
  aircraft's altitude. The GS needle then reflects how high or low you are
  relative to the path. This is the realistic / honest mode.
- **Manual nudge** — Starts on profile, then on-screen `+25 ft` and `−25 ft`
  buttons let you displace the simulated altitude up or down. The offset
  decays back to zero at 5 ft/s, so the needle springs back to centre over
  time. Useful for seeing what fly-up / fly-down looks like.

### Buttons

- **Pause** — Freezes the needles and readouts at their current values
  without disconnecting the position source. Tap again to resume. The button
  stays highlighted while paused.
- **Lock Track** — Appears only after you've moved more than 50 m. Re-derives
  the inbound course from the line you've actually walked / flown, on the
  assumption that your initial heading was a bit off. Useful when device
  compass calibration is poor.
- **Reset** — Stops the simulation, returns to the setup screen with your
  inputs preserved, and disconnects the position source.

### Banners

- **MINIMUMS — DECIDE** (red, flashing) — fires when:
  - **ILS**: current altitude descends through DA on the inbound segment.
  - **LNAV+V**: distance to threshold reaches the MDA point you configured.
  - **LNAV-only**: distance to threshold reaches 0.5 NM (nominal).
- **GO AROUND / TOUCHDOWN** — full-screen overlay when you cross the
  threshold. Tap **Reset** to start again.

---

## MSFS Bridge mode in detail

### How the data flows

```
  MSFS 2024  →  MSFS Bridge  →  UDP/49002  →  msfs-bridge-relay.py  →  WebSocket/9090  →  index.html
```

MSFS Bridge speaks the **ForeFlight XGPS / XATT** text protocol on UDP.
Browsers can't read raw UDP, so the small Python relay listens for those
packets, parses them, and rebroadcasts them as JSON on a WebSocket the
simulator can connect to over plain HTTP/`file://`.

### MSFS Bridge configuration

Configure MSFS Bridge to broadcast to:

- **Host**: the IP of the PC you're running the relay on. If MSFS and the
  relay are on the same PC, `127.0.0.1` is fine.
- **Port**: `49002` (default — and the relay's default).
- **Format**: ForeFlight (XGPS / XATT). This is the default.

Bridge can be running before or after the relay — UDP is fire-and-forget so
the relay starts catching packets the instant it's up.

### Running the relay

```bash
# one-time install
pip install websockets

# run with default ports (UDP 49002 in, WebSocket 9090 out)
python msfs-bridge-relay.py

# log every packet (default), heartbeat every 5 s
python msfs-bridge-relay.py --verbose      # adds full hex + ASCII per packet
python msfs-bridge-relay.py --quiet        # heartbeat only, no per-packet
python msfs-bridge-relay.py --version      # print version and exit
```

You'll see one line per UDP packet showing the parsed XGPS fix, plus a
heartbeat every 5 s with totals and the most recent fix:

```
RX 46B 192.168.1.20:49002  [xgps ]  51.477492,  -0.461404   3928 ft  hdg= 89.5°  gs= 138 kt
RX 28B 192.168.1.20:49002  [xatt ]  hdg= 89.5°  pitch=  2.1°  roll= -1.2°
[heartbeat] rx=42 parsed=21 xatt=21 unparsed=0 rate=8.4/s ws_clients=1  last_fix=51.47749,-0.46140
```

If you see `[UNPARSED]` lines, the bridge is sending a format the relay
doesn't recognise yet — the verbose ASCII line will show what's coming in,
which is enough to diagnose.

### Connecting the simulator

In the setup screen of `index.html`:

1. Set **Location Source** to `MSFS Bridge`.
2. Enter the WebSocket URL:
   - Same PC as relay: `ws://127.0.0.1:9090`
   - Different machine: `ws://<relay-PC-IP>:9090`
3. Fill in the rest of the setup as normal and tap **Go**.

### One thing to be aware of

The simulator anchors its centerline on the **first position fix** received
after Go. If your sim aircraft is moving when you tap Go, you may end up
already past the FAF immediately. For sim use, either:

- Pause MSFS before tapping Go and configure your aircraft at the planned
  starting point, or
- Tap Go at a known initial-approach fix.

---

## Troubleshooting

### "GPS permission denied"

Reload the page and grant location permission. On iOS Safari you may need
to enable Settings → Safari → Location → Allow.

### "No GPS / Bridge fix within 10 s"

- **GPS**: are you indoors or on a desktop without GPS? Try outside or use
  MSFS Bridge mode.
- **Bridge**: is the relay running and reachable? Run `python ws-test.py` from
  the same machine as the simulator — it does a TCP probe, then a WebSocket
  handshake, then prints incoming messages live. If `ws-test.py` works but
  the simulator doesn't, check whether the page is being served over `https://`
  (which blocks plain `ws://` as mixed-content); open via `file://` or plain
  `http://` instead.

### Simulator on phone, relay on PC

The phone needs to reach the PC's LAN IP, not `127.0.0.1`. In the setup
screen, use `ws://<PC-LAN-IP>:9090`. You may need to allow Windows Defender
Firewall to permit inbound TCP/9090.

### Stale `index.html` or relay copy

Both files show their version (`vX.Y.Z`) on startup — the simulator next to
the title and in the corner of the CDI, the relay on the first line of its
banner and via `python msfs-bridge-relay.py --version`. If a fix you expect
isn't taking effect, check the version against this README's history; it's
usually OneDrive sync taking its time. Re-copy the file or open the OneDrive
folder in Explorer to force a sync.

### `[UNPARSED]` packets in the relay

The bridge is sending a format the relay doesn't recognise. Run with
`--verbose` and paste the `ascii:` line — that's enough to add support for
whatever variant it is.

---

## Disclaimer

This is a **training and rehearsal tool**. It does not use real navaid
databases, does not model wind or aircraft performance, and must not be
used for actual flight navigation under any circumstances. Always fly the
real procedure from current published charts.
