'use strict'

const UNAVAILABLE_REASON = 'no validated Dyness B3 RS485 protocol or response'

function finite (value) {
  return typeof value === 'number' && Number.isFinite(value)
}

function unavailableStatus (overrides = {}) {
  return {
    available: false,
    valid: false,
    source: 'rs485-unavailable',
    interface: '/dev/ttyUSB0',
    baudRate: null,
    parity: null,
    slaveAddress: null,
    reason: UNAVAILABLE_REASON,
    ...overrides,
    available: false,
    valid: false
  }
}

module.exports = { UNAVAILABLE_REASON, unavailableStatus }

function normalizeSnapshot (snapshot, now = Date.now(), staleMs = 5000) {
  const age = snapshot && finite(snapshot.timestamp)
    ? Math.max(0, now - snapshot.timestamp)
    : null
  const system = snapshot && snapshot.system ? snapshot.system : {}
  const limits = snapshot && snapshot.limits ? snapshot.limits : {}
  const aggregate = snapshot && snapshot.aggregate ? snapshot.aggregate : {}
  const fresh = age !== null && age <= staleMs
  const cellTelemetryValid = Boolean(snapshot && snapshot.cellTelemetryValid &&
    finite(aggregate.vmax) && finite(aggregate.vmin))
  const valid = Boolean(snapshot && snapshot.valid && fresh && cellTelemetryValid)
  return {
    ...snapshot,
    timestamp: snapshot && snapshot.timestamp,
    telemetryAge: age,
    valid,
    cellTelemetryValid,
    packVoltage: finite(system.voltage61) ? system.voltage61 : null,
    packCurrent: valid && finite(aggregate.summedBatteryCurrent) ? aggregate.summedBatteryCurrent : null,
    soc: finite(system.soc61) ? system.soc61 : null,
    vmax: finite(aggregate.vmax) ? aggregate.vmax : null,
    vmin: finite(aggregate.vmin) ? aggregate.vmin : null,
    maxCellIndex: aggregate.maxCellIndex ?? null,
    minCellIndex: aggregate.minCellIndex ?? null,
    maxCellAddress: aggregate.maxCellAddress ?? null,
    minCellAddress: aggregate.minCellAddress ?? null,
    ccl: finite(limits.chargeCurrent) ? limits.chargeCurrent : null,
    dcl: finite(limits.dischargeCurrentSigned) ? Math.abs(limits.dischargeCurrentSigned) : null,
    cvl: finite(limits.chargeVoltage) ? limits.chargeVoltage : null,
    bmsStatus: limits.statusRaw ?? null,
    bmsStatusFlags: limits.statusFlags || {},
    lockout: valid ? null : 'fresh validated RS485 cell telemetry is required'
  }
}

module.exports = { UNAVAILABLE_REASON, unavailableStatus, normalizeSnapshot }
