'use strict'

const assert = require('assert/strict')
const fs = require('fs')
const vm = require('vm')

const source = fs.readFileSync('src/maintenance_formatter.js', 'utf8')

function format(current, lastValid, now) {
  const values = {
    balancerRs485Snapshot: current,
    balancerRs485LastValidSnapshot: lastValid
  }
  const context = {
    Date: { now: () => now },
    flow: { get: (key) => values[key] },
    msg: {}
  }
  vm.runInNewContext(`(function () { ${source} })()`, context)
  return context.msg.payload
}

const valid = {
  timestamp: 100000,
  valid: true,
  system: {
    voltage61: 54.84,
    current61: 0.1,
    soc61: 100,
    averageCycleCount61: 123,
    maximumCycleCount61: 456,
    averageSoh61: 100,
    minimumSoh61: 99,
    maximumCellVoltage61: 3.524,
    maximumCellId61: 1794,
    minimumCellVoltage61: 3.342,
    minimumCellId61: 2818,
    averageCellTemperature61: 26.1,
    maximumCellTemperature61: 26.4,
    maximumCellTemperatureId61: 1026,
    minimumCellTemperature61: 25.9,
    minimumCellTemperatureId61: 514,
    averageMosfetTemperature61: 27.0,
    maximumMosfetTemperature61: 28.0,
    maximumMosfetTemperatureId61: 258,
    minimumMosfetTemperature61: 26.0,
    minimumMosfetTemperatureId61: 655,
    averageBmsTemperature61: 34.2,
    maximumBmsTemperature61: 34.3,
    maximumBmsTemperatureId61: 258,
    minimumBmsTemperature61: 34.1,
    minimumBmsTemperatureId61: 258
  },
  limits: {
    chargeVoltage: 56.5,
    dischargeVoltage: 48,
    chargeCurrent: 0,
    dischargeCurrentSigned: -198.8,
    statusRaw: 64,
    statusFlags: {
      state: 'permissions',
      chargeEnabled: false,
      dischargeEnabled: true,
      strongCharge: false,
      fullCharge: false,
      unknownReservedBits: 0,
      unknownReservedHex: '0x0',
      active: [{ description: 'discharge enabled' }]
    }
  },
  aggregate: { vmin: 3.346, vmax: 3.582, spread: 0.236 },
  batteries: [],
  inventory: { activeAddresses: [2], pendingRemoval: [], discoveryMode: 'normal', scanIntervalSeconds: 60 },
  serialHealth: { state: 'connected', reconnectCount: 1, ownerConflict: false }
}

const live = format(valid, null, 100000)
assert.equal(live.displayState, 'LIVE')
assert.equal(live.system.voltage, '54.840')
assert.equal(live.system.current, '0.1')
assert.equal(live.system.averageCycleCount, '123')
assert.equal(live.system.maximumCycleCount, '456')
assert.equal(live.system.averageSoh, '100')
assert.equal(live.system.minimumSoh, '99')
assert.equal(live.system.cellSummary.maximumVoltage, '3.524')
assert.equal(live.system.cellSummary.maximumId, '1794')
assert.equal(live.system.cellSummary.minimumId, '2818')
assert.equal(live.system.cellSummary.spread, '0.182')
assert.equal(live.system.cellTemperature.maximumId, '1026')
assert.equal(live.system.mosfetTemperature.minimumId, '655')
assert.equal(live.system.bmsTemperature.average, '34.2')
assert.equal(live.system.bmsTemperature.maximumId, '258')
assert.equal(live.system.bmsTemperature.single, null)
const singleBms = format({
  ...valid,
  system: {
    ...valid.system,
    averageBmsTemperature61: null,
    maximumBmsTemperature61: 34.2,
    minimumBmsTemperature61: 34.2
  }
}, null, 100000)
assert.equal(singleBms.system.bmsTemperature.single.value, '34.2')
assert.equal(singleBms.system.bmsTemperature.single.id, '258')

const status44 = {
  available: true,
  timestamp: 100000,
  status1: { raw: 0, active: [], reserved: { bits: 0, hex: '0x00' } },
  status2: { raw: 15, prechargeMosfet: true, chargeMosfet: true, dischargeMosfet: true, modulePowerActive: true, reserved: { bits: 0, hex: '0x00' } },
  status3: { raw: 201, effectiveCharging: true, effectiveDischarging: true, heaterActive: false, fullyCharged: true, buzzerActive: true, reserved: { bits: 0, hex: '0x00' } },
  status4: { raw: 0, cellFaults: [] },
  status5: { raw: 0, cellFaults: [] },
  alarms: { cell: [], temperature: [], chargeCurrent: 0, moduleVoltage: 0, dischargeCurrent: 0 }
}
const statusLive = format({
  ...valid,
  batteries: [{ address: 2, valid: true, voltage: 54.49, current: 0.1, effectiveCells: [], temperatures: [], status44 }]
}, null, 100000)
assert.equal(statusLive.batteries[0].status44.state, 'OK')
assert.equal(statusLive.batteries[0].status44.stateClass, 'good')
assert.equal(statusLive.batteries[0].status44.raw, 'S1 0x00 · S2 0x0F · S3 0xC9 · S4 0x00 · S5 0x00')
assert.equal(statusLive.batteries[0].status44.chips[0].value, 'ON')
assert.equal(statusLive.batteries[0].status44.protection, 'None')
assert.equal(statusLive.batteries[0].status44.cellFaults, 'Cells 1–8: None · Cells 9–16: None')

const statusWarn = format({
  ...valid,
  batteries: [{
    address: 2,
    valid: true,
    voltage: 54.49,
    current: 0.1,
    effectiveCells: [],
    temperatures: [],
    status44: {
      ...status44,
      status1: { raw: 1, active: [{ description: 'over-voltage protection' }], reserved: { bits: 0, hex: '0x00' } },
      status4: { raw: 1, cellFaults: [1] },
      alarms: { cell: [2], temperature: [], chargeCurrent: 0, moduleVoltage: 0, dischargeCurrent: 0 }
    }
  }]
}, null, 100000)
assert.equal(statusWarn.batteries[0].status44.state, 'WARN')
assert.equal(statusWarn.batteries[0].status44.stateClass, 'bad')
assert.equal(statusWarn.batteries[0].status44.protection, 'over-voltage protection')
assert.equal(statusWarn.batteries[0].status44.cellFaults, 'Cells 1–8: 1 · Cells 9–16: None')

const statusUnavailable = format({
  ...valid,
  batteries: [{ address: 2, valid: true, effectiveCells: [], temperatures: [], status44: { available: false, timestamp: 100000, error: 'CID2=44 timeout' } }]
}, null, 100000)
assert.equal(statusUnavailable.batteries[0].status44.state, 'UNAVAILABLE')
assert.equal(statusUnavailable.batteries[0].status44.stateClass, 'neutral')
assert.equal(statusUnavailable.batteries[0].status44.error, 'CID2=44 timeout')
assert.equal(live.valid, true)
assert.equal(live.limits.status, '0x40')
assert.deepEqual(live.limits.statusActive, ['discharge enabled'])
assert.equal(live.limits.statusFlags.chargeEnabled, false)

const stale = format({ ...valid, timestamp: 101000, valid: false, reason: 'CID2=63 timeout' }, valid, 105000)
assert.equal(stale.displayState, 'STALE')
assert.equal(stale.system.voltage, '54.840')
assert.equal(stale.valid, false)
assert.equal(stale.reason, 'CID2=63 timeout')

const expired = format({ ...valid, timestamp: 111000, valid: false }, valid, 111001)
assert.equal(expired.displayState, 'INVALID')
assert.equal(expired.system.voltage, '—')

const invalidPhysical = format({
  ...valid,
  system: {
    ...valid.system,
    averageCycleCount61: 65535,
    minimumSoh61: 255,
    averageCellTemperature61: 6280.4,
    maximumCellTemperature61: 6280.4,
    maximumCellTemperatureId61: 1026
  }
}, null, 100000)
assert.equal(invalidPhysical.system.averageCycleCount, '—')
assert.equal(invalidPhysical.system.minimumSoh, '—')
assert.equal(invalidPhysical.system.cellTemperature.average, '—')
assert.equal(invalidPhysical.system.cellTemperature.maximum, '—')
assert.equal(invalidPhysical.system.cellTemperature.maximumId, '—')
