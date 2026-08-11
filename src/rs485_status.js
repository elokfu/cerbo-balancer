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

function normalizeSnapshot (snapshot, now = Date.now(), staleMs = 20000) {
  const age = snapshot && finite(snapshot.timestamp)
    ? Math.max(0, now - snapshot.timestamp)
    : null
  const system = snapshot && snapshot.system ? snapshot.system : {}
  const limits = snapshot && snapshot.limits ? snapshot.limits : {}
  const aggregate = snapshot && snapshot.aggregate ? snapshot.aggregate : {}
  const fresh = age !== null && age <= staleMs
  const cellTelemetryValid = Boolean(snapshot && snapshot.cellTelemetryValid &&
    finite(aggregate.vmax) && finite(aggregate.vmin))
  const expected = snapshot && Array.isArray(snapshot.expectedAddresses) ? snapshot.expectedAddresses : []
  const batteries = snapshot && Array.isArray(snapshot.batteries) ? snapshot.batteries : []
  const addressedSystemTelemetryValid = expected.length > 0 && expected.every(address => {
    const battery = batteries.find(item => item.address === address)
    return Boolean(battery && battery.system61Valid === true && Number.isInteger(battery.soc) &&
      finite(battery.vmin) && finite(battery.vmax) && finite(battery.spread))
  })
  const valid = Boolean(snapshot && snapshot.valid && fresh && cellTelemetryValid && addressedSystemTelemetryValid)
  return {
    ...snapshot,
    timestamp: snapshot && snapshot.timestamp,
    telemetryAge: age,
    valid,
    cellTelemetryValid,
    addressedSystemTelemetryValid,
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
    expectedBatteries: expected,
    lockout: valid ? null : 'complete fresh addressed CID2 61 telemetry is required'
  }
}

module.exports = { UNAVAILABLE_REASON, unavailableStatus, normalizeSnapshot }
