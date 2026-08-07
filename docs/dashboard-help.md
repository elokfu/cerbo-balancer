## Dyness Balancer

**TEST** is the startup mode. TEST calculates the shadow pack-voltage
command, feed-forward term, PI terms, safety overrides, and BMS-limit
arbitration, but performs no voltage write.

**ACTIVE** is guarded by the controller. It requires fresh and independently
validated cell telemetry, valid configuration, configured PI gains, and
verified output readback. The current direct-CAN adapter remains unavailable
until cell values are validated; therefore ACTIVE should remain locked out.

The controller is enabled only when explicitly selected. `AUTO` evaluates
equalization entry and completion; `MANUAL` uses Start/Stop. Safety handling
and stale-input protection apply in every mode.

The displayed spread is Vmax minus Vmin. Cell indexes are only shown as
validated values. Raw CAN frames and decoder health are retained for
diagnostics. A CCL of zero is logged as an advertised limit and is not treated
as a disconnected battery by itself.
