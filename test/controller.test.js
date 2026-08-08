'use strict'

const assert = require('assert/strict')
const { STATES, MODES, createBalancerController, validateConfig } = require('../src/controller')

let clock = 1000000
const controller = createBalancerController({ now: () => clock })

function battery (overrides = {}) {
  const cells = [3.46, 3.42, 3.45, 3.44, 3.43, 3.42, 3.455, 3.43, 3.44, 3.45, 3.425, 3.43, 3.44, 3.43, 3.44, 3.435]
  return {
    address: 2, valid: true, current: 0.5, cellSpread: 0.040, effectiveCells: cells.map((voltage, index) => ({ index: index + 1, voltage })),
    status44: { status1: { active: [] }, status2: { chargeMosfet: true } }, ...overrides
  }
}
function telemetry (overrides = {}) {
  return {
    timestamp: clock, valid: true, cellTelemetryValid: true, soc: 99, vmax: 3.455, vmin: 3.42,
    ccl: 20, cvl: 56.5, limits: { statusFlags: { chargeEnabled: true, dischargeEnabled: true } },
    batteries: [battery()], expectedBatteries: [2], ...overrides
  }
}
function command (actions) { return actions.find(action => action.type === 'controller_command').value }

assert.equal(validateConfig({}).valid, true)
assert.equal(validateConfig({ socEntryThreshold: 97, socExitThreshold: 98 }).valid, false)
controller.handle({ type: 'telemetry', telemetry: telemetry(), timestamp: clock })
controller.handle({ type: 'set_enabled', value: true, timestamp: clock })
controller.handle({ type: 'tick', timestamp: clock })
assert.equal(controller.getState().state, STATES.FAULT_OR_ABORT) // output readback is deliberately required

const active = createBalancerController({ now: () => clock })
active.handle({ type: 'telemetry', telemetry: telemetry(), timestamp: clock })
active.handle({ type: 'set_output_ready', value: true, timestamp: clock })
active.handle({ type: 'set_enabled', value: true, timestamp: clock })
let actions = active.handle({ type: 'tick', timestamp: clock })
assert.equal(active.getState().state, STATES.BALANCE_RESTART)
clock += 2000
actions = active.handle({ type: 'tick', timestamp: clock })
assert.equal(command(actions).chargeEnabled, true)
assert.equal(command(actions).requestedCurrent <= 20, true)

clock += 2000
actions = active.handle({ type: 'telemetry', timestamp: clock, telemetry: telemetry({ ccl: 0 }) })
assert.equal(active.getState().state, STATES.BALANCE_DISCHARGE_RECOVERY)
assert.equal(active.getState().lastStopReason, 'CCL_ZERO_STOP')
assert.equal(command(actions).chargeEnabled, false)

clock += 2000
actions = active.handle({ type: 'telemetry', timestamp: clock, telemetry: telemetry({ soc: 97, ccl: 20 }) })
assert.equal(active.getState().state, STATES.NORMAL)
assert.equal(active.getState().lastStopReason, 'SOC_EXIT')
assert.equal(command(actions).chargeEnabled, true)

const noStart = createBalancerController({ now: () => clock })
noStart.handle({ type: 'telemetry', timestamp: clock, telemetry: telemetry({ soc: 98 }) })
noStart.handle({ type: 'set_output_ready', value: true, timestamp: clock })
noStart.handle({ type: 'set_enabled', value: true, timestamp: clock })
assert.equal(noStart.getState().state, STATES.NORMAL)

const testMode = createBalancerController({ now: () => clock })
testMode.handle({ type: 'telemetry', timestamp: clock, telemetry: telemetry() })
testMode.handle({ type: 'set_output_ready', value: true, timestamp: clock })
testMode.handle({ type: 'set_enabled', value: true, timestamp: clock })
assert.equal(testMode.getStatus().mode, MODES.TEST)
assert.equal(command(testMode.handle({ type: 'tick', timestamp: clock })).mode, MODES.TEST)
