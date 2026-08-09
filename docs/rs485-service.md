# RS485 service and virtual BMS

`scripts/dyness_rs485_service.py` polls the stable FTDI path at 115200 8N1.
It discovers Dyness/Pylon addresses 2–16 and sends only read requests:

- CID2 `0x42`: per-battery cells, temperatures, current, and voltage.
- CID2 `0x61`: complete system summary, including system current, SOC, cell
  summary, and cell/MOSFET/BMS temperatures.
- CID2 `0x63`: charge/discharge voltage, CCL, DCL, and permission/state bits.
- CID2 `0x44`: per-battery alarms plus complete Status1–Status5 decoding.

CID2 `0x42` capacity/lifetime tails are decoded when their structural markers
are valid: cycle count, remaining capacity in Ah, total capacity in Ah, and
capacity-derived SOC. These optional fields are published with each battery;
missing or invalid tails do not invalidate otherwise valid cell telemetry.

CID2 `0x44` is polled for active batteries every five seconds. Status1
contains protection flags; Status2 contains precharge, charge/discharge
MOSFET, and module-power states; Status3 contains effective charging,
effective discharging, heater, full-charge, and buzzer states. Status4 and
Status5 identify cell voltage-check faults for cells 1–8 and 9–16. These
values are diagnostic-only and do not alter telemetry validity, DVCC limits,
or controller behavior.

The CID2 `0x63` status byte is decoded and retained as `limits.statusFlags`.
The correct Pylon/Dyness meanings are: bit 7 charge enabled, bit 6 discharge
enabled, bit 5 strong charge, and bit 4 full charge. Bits 0–3 are retained as
`unknownReservedBits` and are displayed as reserved; they do not create
invented alarms or protection states. The raw byte remains in `statusRaw`.

The virtual BMS uses the permission bits for `/Bms/AllowToCharge` and
`/Bms/AllowToDischarge`, together with the advertised current limits. A zero
CCL is valid and means charging is not permitted; it is not treated as a
disconnect. In a fresh ACTIVE controller command, `chargeEnabled=false`
additionally clamps charge current to zero and `/Bms/AllowToCharge` false,
without changing Dyness discharge permission or DCL. The management page
displays all four permission/state bits and the reserved low bits.

CID2 `0x61` temperatures are range-validated before publication. Sentinel
values such as `0xFFFF` and decoded values outside -40 to 100 °C become
unavailable and are never displayed. The maintenance page explicitly shows
the valid average, minimum, and maximum BMS temperatures; per-battery sensor
temperatures remain sourced from CID2 `0x42`.

The adapter is optional during development. A root-supervised runit wrapper
owns `ttyUSB0`, invokes Venus' official `stop-tty.sh ttyUSB0` handoff, and
starts the Python poller as `nodered`. Node-RED only reads the latest telemetry
JSONL line; it does not launch another serial client. If the adapter is
disconnected, the service emits a timestamped invalid snapshot, keeps CCL/DCL
at zero, and does not allow ACTIVE controller operation.

The Python poller keeps one serial session open, requests exclusive access when
supported, and reconnects with a two-second backoff after USB or serial errors.
The supervisor reapplies ownership after the stable FTDI path reappears or a
generic Venus service reclaims `ttyUSB0`. Snapshots expose serial state, owner
conflict, reconnect count, last valid timestamp, poll duration, and the last
classified error.

Inventory recovery is separate from normal telemetry polling. A complete
address scan covers addresses 2–16 every 60 seconds during normal operation.
Only active responding addresses receive CID2 `0x42` data polls. If a known
battery disappears during a full scan, it is removed from active polling and
retained as pending removal. Full recovery scans run every 10 seconds while a
battery is pending; removal occurs after 10 consecutive failed scans. A
returning or newly discovered battery is activated immediately. Inventory and
missed-scan counters are persisted in
`/data/home/nodered/cerbo-balancer-rs485-inventory.json`.

The service owns the virtual D-Bus name
`com.victronenergy.battery.rs485_dyness` with DeviceInstance `100`. Once that
service is separately selected as the active battery monitor, GX displays:

- `/Soc` from CID2 `0x61`;
- `/Dc/0/Voltage` from CID2 `0x61`;
- `/Dc/0/Current` as the complete sum of valid CID2 `0x42` battery currents;
- `/Dc/0/Power` as voltage multiplied by summed current.

No partial current sum is published as authoritative. D-Bus publishing does
not modify DVCC, charger settings, or battery configuration.

Serial or USB failures do not count as inventory-removal scans. Only a
completed full address scan can move a known battery into pending removal.

For a Cerbo installation, copy both Python files in `scripts/` to the runtime
directory, install the runit files from `deploy/`, and let the root-supervised
service own the serial adapter. The generated Node-RED flow reads the latest
JSON snapshot for the maintenance page; it does not start the Python service.

## CSV session logging

CSV logging is controlled from the balancer page and writes files below
`/data/home/nodered/cerbo-balancer-csv/`. When a file is first created, its
schema is fixed from the active battery inventory at that moment. Batteries
discovered later are ignored and do not add columns. If any battery in the
initial inventory is absent from a later sample, the service stops that
recording session rather than writing partial rows.

The CSV header contains the constant serial metadata, virtual-BMS identity,
shadow-mode marker, and initial battery addresses. Each battery contributes 16
cell-voltage fields and five temperature fields. Cell voltages are always
written in volts with three decimal places. Pack voltages, temperatures,
currents, and spreads are written with two decimal places, except pack Vmin/Vmax
which use three decimal places; spreads are whole millivolts. Per-battery
columns precede the raw BMS and controller-arbitration columns. The trailing
control columns record the controller-requested CVL/CCL/charge state and the
final virtual-BMS effective CVL/CCL/DCL, permissions, thermal factor, command
freshness, and arbitration reason. Each data row uses local Cerbo time as
`HH:MM:SS` in the configured `CERBO_BALANCER_TIMEZONE` (default
`Europe/Berlin`) and includes a monotonic `sample_number`, so recordings longer than
24 hours remain unambiguous without storing a calendar date.
