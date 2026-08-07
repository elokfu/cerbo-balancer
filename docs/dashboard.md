# Dashboard

The page is `/dashboard/balancer`. It exposes mode, enable, automatic/manual
selection, guarded ACTIVE request, state, lockout reason, alarms, D-Bus
telemetry, raw CAN health, diagnostic controller terms, graphs, manual start
and stop, integrator reset, and restore-default controls.

Mode starts at `TEST`. The ACTIVE control is intentionally rejected unless
cell telemetry and output readback are verified. TEST commands are diagnostic
intent only; no physical charger or voltage setting is changed.
