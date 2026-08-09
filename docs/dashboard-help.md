## Dyness Balancer

**TEST** is the startup mode. TEST calculates selected-battery current PI,
SOC hysteresis, Vmax/CCL stops, discharge recovery, and BMS-limit arbitration,
but performs no DVCC or charger write.

**ACTIVE** is guarded by the controller. It requires fresh and independently
validated cell telemetry, valid configuration, and verified output readback.
Physical activation additionally requires the separate DVCC commissioning
approval.

TEST mode is shadow-only: it calculates and logs controller commands without
requiring output readback and without writing charger or voltage settings.

The controller is enabled only when explicitly selected. `AUTO` evaluates
automatic balancing entry and completion; `MANUAL` uses Start/Stop. New
automatic sequences require SOC above 98%; a selected sequence continues above
97% and exits at or below 97%. Safety handling and stale-input protection apply
in every mode.

Pack-wide Vmin, Vmax, spread, and their battery/cell locations are from the
CID2 `0x61` system summary. Per-battery Vmin, Vmax, and spread are calculated
from that battery's validated CID2 `0x42` cell array. If CID2 `0x61` extrema
are unavailable or unphysical, pack-wide extrema are shown as unavailable and
elevated balancing is blocked; the controller never substitutes locally
calculated values. A CCL of zero is not treated as a disconnected battery, but
it does stop an active balancing charge interval and enters discharge recovery.
