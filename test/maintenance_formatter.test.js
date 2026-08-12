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
    minimumMosfetTemperatureId61: 514,
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
  aggregate: {
    source: 'CID2_61_SYSTEM_SUMMARY',
    valid: true,
    vmin: 3.342,
    vmax: 3.524,
    spread: 0.182,
    minCellAddress: 2,
    minCellIndex: 11,
    maxCellAddress: 2,
    maxCellIndex: 7
  },
  batteries: [{
    address: 2,
    valid: true,
    soc: 100,
    vmin: 3.342,
    vminId: 0x0B02,
    vmax: 3.524,
    vmaxId: 0x0702,
    spread: 0.182,
    voltage: 54.49,
    current: 0.1,
    temperatures: [26.1, 25.9, 26.1, 26.4, 26.3],
    cycleCount: 12,
    remainingCapacityAh: 277.2,
    totalCapacityAh: 280.0,
    capacitySoc: 99.0,
    effectiveCells: []
  }],
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
assert.equal(live.system.cycleCount, '12')
assert.equal(live.system.remainingCapacity, '277.2')
assert.equal(live.system.totalCapacity, '280.0')
assert.equal(live.system.cellSummary.maximumVoltage, '3.524')
assert.equal(live.system.cellSummary.maximumId, 'Battery 2 · Cell 07')
assert.equal(live.system.cellSummary.minimumId, 'Battery 2 · Cell 11')
assert.equal(live.system.cellSummary.maximumRawId, '0x0702')
assert.equal(live.system.cellSummary.minimumRawId, '0x0B02')
assert.equal(live.system.cellSummary.spread, '0.182')
assert.equal(live.aggregate.source, 'CID2 61 system summary')
assert.equal(live.system.cellTemperature.maximumId, 'Battery 2 · Sensor 04')
assert.equal(live.system.mosfetTemperature.minimumId, 'Battery 2 · Sensor 02')
assert.equal(live.system.bmsTemperature.average, '34.2')
assert.equal(live.system.bmsTemperature.maximumId, 'Battery 2 · Sensor 01')
assert.equal(live.system.bmsTemperature.single, null)
assert.equal(live.batteries[0].soc, '100')
assert.equal(live.batteries[0].vmin, '3.342')
assert.equal(live.batteries[0].vmax, '3.524')
assert.equal(live.batteries[0].cellSpread, '0.182')
assert.equal(live.batteries[0].cellExtremaSource, 'addressed CID2 61')
assert.equal(live.batteries[0].vminLocation, 'Battery 2 · Cell 11')

const effectiveCells = (values) => values.map((voltage, index) => ({ index: index + 1, voltage }))
const chargingStatus = {
  available: true,
  timestamp: 100000,
  status2: { chargeMosfet: true },
  status3: { effectiveCharging: true }
}
const unbalancedCounts = format({
  ...valid,
  batteries: [
    {
      address: 2,
      valid: true,
      current: 1.5,
      status44: chargingStatus,
      effectiveCells: effectiveCells([3.331, 3.302, 3.301, 3.300, ...Array(12).fill(3.331)])
    },
    {
      address: 3,
      valid: true,
      current: 2.0,
      status44: chargingStatus,
      effectiveCells: effectiveCells([3.500, 3.471, 3.470, 3.469, 3.469, ...Array(11).fill(3.500)])
    },
    {
      address: 4,
      valid: false,
      effectiveCells: effectiveCells(Array(16).fill(3.500))
    },
    {
      address: 5,
      valid: true,
      effectiveCells: effectiveCells(Array(15).fill(3.500))
    }
  ]
}, null, 100000)
assert.equal(unbalancedCounts.aggregate.unbalancedCellCount, '3')
assert.equal(unbalancedCounts.aggregate.balancingCellCount, '25')
assert.equal(unbalancedCounts.batteries[0].unbalancedCellCount, '1')
assert.equal(unbalancedCounts.batteries[1].unbalancedCellCount, '2')
assert.equal(unbalancedCounts.batteries[0].balancingCellCount, '13')
assert.equal(unbalancedCounts.batteries[1].balancingCellCount, '12')
assert.equal(unbalancedCounts.batteries[2].unbalancedCellCount, '—')
assert.equal(unbalancedCounts.batteries[3].unbalancedCellCount, '—')
assert.equal(unbalancedCounts.batteries[2].balancingCellCount, '—')
assert.equal(unbalancedCounts.batteries[3].balancingCellCount, '—')

const balancingCriteria = format({
  ...valid,
  batteries: [
    {
      address: 2,
      valid: true,
      current: 2.0,
      status44: { ...chargingStatus, status2: { chargeMosfet: false } },
      effectiveCells: effectiveCells([3.300, ...Array(15).fill(3.331)])
    },
    {
      address: 3,
      valid: true,
      current: 2.0,
      status44: { ...chargingStatus, status3: { effectiveCharging: false } },
      effectiveCells: effectiveCells([3.300, ...Array(15).fill(3.331)])
    },
    {
      address: 4,
      valid: true,
      current: 1.49,
      status44: chargingStatus,
      effectiveCells: effectiveCells([3.300, ...Array(15).fill(3.331)])
    },
    {
      address: 5,
      valid: true,
      current: 2.0,
      status44: { ...chargingStatus, timestamp: 89999 },
      effectiveCells: effectiveCells([3.300, ...Array(15).fill(3.331)])
    }
  ]
}, null, 100000)
assert.deepEqual(Array.from(balancingCriteria.batteries, (battery) => battery.balancingCellCount), ['0', '0', '0', '0'])
assert.deepEqual(Array.from(balancingCriteria.batteries, (battery) => battery.unbalancedCellCount), ['1', '1', '1', '1'])
assert.equal(balancingCriteria.aggregate.balancingCellCount, '0')
assert.equal(balancingCriteria.aggregate.unbalancedCellCount, '4')

const packedCellId = format({
  ...valid,
  system: { ...valid.system, maximumCellId61: 0x1102 }
}, null, 100000)
assert.equal(packedCellId.system.cellSummary.maximumId, 'Battery 2 · Cell 11')
assert.equal(packedCellId.system.cellSummary.maximumRawId, '0x1102')
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
assert.equal(singleBms.system.bmsTemperature.single.id, 'Battery 2 · Sensor 01')

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
assert.equal(statusLive.batteries[0].status44.chips[0].label, 'Charge MOSFET')
assert.equal(statusLive.batteries[0].status44.chips[0].value, 'ON')
assert.equal(statusLive.batteries[0].status44.flags.length, 20)
assert.equal(statusLive.batteries[0].status44.flags.find((item) => item.label === 'Over-voltage protection').value, 'OFF')
assert.equal(statusLive.batteries[0].status44.flags.find((item) => item.label === 'Fully charged').value, 'ON')
assert.equal(statusLive.batteries[0].status44.protection, 'None')
assert.equal(statusLive.batteries[0].status44.cellFaults, 'Status4, cells 1–8: None · Status5, cells 9–16: None')

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
assert.equal(statusWarn.batteries[0].status44.cellFaults, 'Status4, cells 1–8: 1 · Status5, cells 9–16: None')

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
assert.equal(invalidPhysical.system.cellTemperature.average, '26.2')
assert.equal(invalidPhysical.system.cellTemperature.maximum, '—')
assert.equal(invalidPhysical.system.cellTemperature.maximumId, '—')
