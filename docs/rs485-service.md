# RS485 service and virtual BMS

`scripts/dyness_rs485_service.py` polls the stable FTDI path at 115200 8N1.
It discovers Dyness/Pylon addresses 2–16 and sends only read requests:

- CID2 `0x42`: per-battery cells, temperatures, current, and voltage.
- CID2 `0x61`: system voltage and SOC cross-check.
- CID2 `0x63`: charge/discharge voltage, CCL, DCL, and status.

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
