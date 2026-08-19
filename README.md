# Cerbo Dyness Balancer

Dyness/Pylon-compatible RS485 telemetry and an active-balancing
controller for Cerbo GX. The RS485 adapter is polled at 115200 8N1 using only
CID2 `0x42`, `0x44`, `0x61`, and `0x63` read requests.

CID2 `0x42` is authoritative for each battery's cells, voltage, signed
current, and temperatures. When 15 cells are returned, cell 16 is
reconstructed per battery from that battery's own pack voltage as a quantized
constraint (about 10 mV resolution): a per-battery predictor advances cell 16
by the median movement of the reported cells and clamps it into the ±5 mV
pack-voltage window, seeded with the median reported cell on the first sample
and reset after communication loss, invalid telemetry, or a pack-voltage jump
larger than 0.5 V. CID2 `0x61` is a master/system-summary response on this
Dyness installation: only the master battery at address 2 answers it, and it
supplies the system SOC, voltage, Vmin, Vmax, spread, and packed locations.
Slave addresses do not answer CID2 `0x61` and are never polled for it, so
their expected no-reply is not treated as a communication fault. Per-battery
Vmin, Vmax, and spread are derived from each battery's own CID2 `0x42` cell
array (using the filtered cell-16 estimate); per-battery SOC falls back to
the master's CID2 `0x61` value when a battery has no addressed SOC.

## Current commissioning boundary

The flow calculates a feed-forward plus slow-PI aggregate-current request that
targets 2 A in the first battery whose per-battery spread is strictly above
30 mV. Cerbo's manually selected battery monitor is the authority gate:
instance `100` applies fresh controller requests, while the CAN BMS keeps them
as shadow diagnostics. Unknown selection also remains non-authoritative.

The selected battery remains locked until all expected batteries report integer
SOC 100 (using the master SOC fallback), a qualifying effective-discharge
completion occurs, or its local charge path/protection excludes it.
Cloud-limited charging freezes control without changing state. There are no
forced discharge cycles or software cell voltage stops. Master CID2 `0x63`
permission and limits remain authoritative.

Automatic balancing defaults to **ON** after a fresh state, state reset, or
Restore Defaults. It automatically selects the first eligible battery; there
is no manual Start/Stop operation. Turning it OFF immediately releases the
selection, resets feed-forward and PI, and keeps the controller in `NORMAL`
using the Cerbo Charge Control voltage/current request. The virtual BMS still
enforces Dyness, thermal, permission, and telemetry safety constraints. Select
the normal Dyness CAN BMS manually in Cerbo when RS485 virtual-BMS authority is
not wanted. Ordinary restarts preserve the selected automatic-balancing state.

Production PI defaults are feed-forward gain `1.0`, Kp `0.20`, Ki `0.02`, and
a 10 A/min maximum upward request slew. The dashboard protects unsaved
configuration edits from telemetry refresh and confirms Apply requests before
replacing the editor values.

In `NORMAL`, requested charge voltage and current come from the Cerbo's
read-only **Settings → System Setup → Charge Control** values. The virtual BMS
also treats enabled UI limits as ceilings during balancing, in addition to the
Dyness BMS and thermal limits. Changing the GX settings therefore takes effect
on the next telemetry cycle without editing balancer configuration.

Run locally:

```text
npm test
npm run build-flow
npm run check
```

The root-supervised RS485 service owns the optional FTDI adapter and starts the
read-only Python poller as `nodered`. Node-RED reads the latest JSONL snapshot;
it does not launch a second serial client. The generated flow is
`flow/cerbo-balancer-controller.json`. Import it by merging it with the
existing Node-RED flow, retaining the existing `victron-client` configuration
node. Dashboards are available at `/dashboard/balancer` and
`/dashboard/balancer-maintenance`. The maintenance page refreshes every five
seconds and shows system SOC/voltage, limits, per-battery voltage/current,
cell voltages, per-battery and all-pack spread, temperatures, inventory
recovery status, serial ownership, reconnect count, and USB/serial errors.
Short interruptions show the last valid measurements as `STALE` for up to ten
seconds; controller safety remains invalid during that period.

Persistent runtime files are stored on Cerbo under `/data/home/nodered/`:

```text
/data/home/nodered/cerbo-balancer-config.json
/data/home/nodered/cerbo-balancer-state.json
/data/home/nodered/cerbo-balancer-rs485-inventory.json
/data/home/nodered/cerbo-balancer-events.jsonl
/data/home/nodered/cerbo-balancer-sessions.jsonl
```

Detailed RS485 telemetry is written as parsed hourly JSONL segments below
`/data/home/nodered/cerbo-balancer-telemetry/` and retained for 24 hours. The
latest parsed snapshot is `/data/home/nodered/cerbo-balancer-latest.json`.
The compact monthly summary is written every 60 seconds to
`/data/home/nodered/cerbo-balancer-summary.jsonl` and retained for 30 days.
Raw RS485 frames and protocol payloads are not persisted.

Install `deploy/cerbo-balancer-rs485-run` as a root-supervised runit service.
It exclusively claims `ttyUSB0`, stops Venus generic serial services assigned
to that port, and repeats the handoff after a USB reconnect. CAN, DVCC, and
unrelated serial ports are not changed.

No credentials are stored in this repository.

The service publishes the RS485 virtual BMS as DeviceInstance `100` alongside
the normal Dyness CAN BMS at instance `512`. Cerbo selection is manual at
`Settings → System Setup → Batteries → Battery monitor`; the service never
changes it. Standard D-Bus limit paths show final effective virtual-BMS
outputs, while the balancer page and logs show requested-versus-effective
arbitration details. The current CAN selection remains the default until the
commissioning handover is deliberately performed.

See [RS485 investigation](docs/rs485-investigation.md), [dashboard operation](docs/dashboard.md), and [DVCC handover](docs/dvcc-handover.md).

## Deployment references

Captured live deployment artifacts are kept in this repository:

- `deploy/node-red-flows-latest.json` — the full live Node-RED flow snapshot
  (balancer and hot-water tabs) as deployed on the Cerbo.
- `deploy/deploy-cerbo-nodered.sh` — deploys that snapshot to the Cerbo's
  Node-RED (`/data/home/nodered/flows.json`) with backup, validation, and
  rollback hints. Dry-run with `--dry-run`; the flow source defaults to
  `node-red-flows-latest.json` next to the script.
- `deploy/cerbo-rc.local` — current `/data/rc.local` on the Cerbo (registers
  the persistent RS485 balancer service).
- `deploy/cerbo-rc.local.bak-before-spy-removal-20260818` — the pre-removal
  boot config including the former `dyness-can-spy` registration.
- `scripts/dyness_pylon_cells.py` — read-only Dyness/Pylon RS485 cell probe.
- `docs/reference/dyness_rs485_service.py.before-master-only-61-20260817-070515`
  — the service as it was before the master-only CID2 0x61 change.
- `archive/dyness-can-spy/` — the removed CAN spy application and capture data
  (preserved for reference; the service is no longer installed on the Cerbo).

Live runtime state files (`cerbo-balancer-*.json`, `.jsonl`, CSV sessions) are
regenerated by the service on the Cerbo and are intentionally not committed.

## Engineering algorithm reference PDF

The generated operator and engineering reference is
[`output/pdf/dyness-balancer-algorithm-reference-with-toc.pdf`](output/pdf/dyness-balancer-algorithm-reference-with-toc.pdf).
Its source is [`docs/dyness-balancer-implementation.md`](docs/dyness-balancer-implementation.md).
It comprehensively documents the PowerBrick PRO `00110` DIP setting,
115200-baud RS485 protocol, Cell 16 estimator, per-battery capacity SOC,
balancing and dynamic-float algorithms, guardian instance 101, D-Bus/DVCC
arbitration, persistence, deployment, rollback, and acceptance checks.

Regenerate it with ReportLab:

```powershell
python docs/pdf/generate_dyness_balancer_pdf.py
```

The generator uses vector ReportLab diagrams and writes the stable output name
under `output/pdf/`.

## GitHub Pages publishing

The search-optimized project page is [`docs/index.html`](docs/index.html).
It includes technical advantages, lifetime-claim boundaries, implementation
architecture, structured FAQ data, crawler metadata, and links to the complete
engineering PDF. Publish it through **Settings -> Pages -> Deploy from a
branch**, using branch `main` and folder `/docs` after merging the feature
branch.
