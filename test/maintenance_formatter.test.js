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
  system: { voltage61: 54.84, soc61: 100 },
  limits: { chargeVoltage: 56.5, dischargeVoltage: 48, chargeCurrent: 0, dischargeCurrentSigned: -198.8 },
  aggregate: { vmin: 3.346, vmax: 3.582, spread: 0.236 },
  batteries: [],
  inventory: { activeAddresses: [2], pendingRemoval: [], discoveryMode: 'normal', scanIntervalSeconds: 60 },
  serialHealth: { state: 'connected', reconnectCount: 1, ownerConflict: false }
}

const live = format(valid, null, 100000)
assert.equal(live.displayState, 'LIVE')
assert.equal(live.system.voltage, '54.840')
assert.equal(live.valid, true)

const stale = format({ ...valid, timestamp: 101000, valid: false, reason: 'CID2=63 timeout' }, valid, 105000)
assert.equal(stale.displayState, 'STALE')
assert.equal(stale.system.voltage, '54.840')
assert.equal(stale.valid, false)
assert.equal(stale.reason, 'CID2=63 timeout')

const expired = format({ ...valid, timestamp: 111000, valid: false }, valid, 111001)
assert.equal(expired.displayState, 'INVALID')
assert.equal(expired.system.voltage, '—')
