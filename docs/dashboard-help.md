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

The displayed spread is Vmax minus Vmin. Cell indexes are only shown as
validated values. Raw CAN frames and decoder health are retained for
diagnostics. A CCL of zero is not treated as a disconnected battery, but it
does stop an active balancing charge interval and enters discharge recovery.
