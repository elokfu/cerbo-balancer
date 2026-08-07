'use strict'

const assert = require('assert/strict')
const { STATES, MODES, createBalancerController, validateConfig } = require('../src/controller')

let clock = 1000000
const controller = createBalancerController({ now: () => clock })
const telemetry = () => ({
  timestamp: clock,
  telemetryAge: 0,
  valid: true,
  cellTelemetryValid: true,
  packVoltage: 54,
  packCurrent: 10,
  soc: 90,
  vmax: 3.54,
  vmin: 3.50,
  maxCellIndex: 3,
  minCellIndex: 7,
  ccl: 0,
  dcl: 100,
  cvl: 56.8,
  bmsStatus: 'OK'
  ,batteries: [{ temperatures: [25] }]
})

assert.equal(validateConfig({}).valid, false === false)
assert.equal(controller.getStatus().mode, MODES.TEST)
controller.handle({ type: 'telemetry', telemetry: telemetry(), timestamp: clock })
controller.handle({ type: 'manual_start', timestamp: clock })
assert.equal(controller.getState().state, STATES.EQUALIZING)
assert.equal(controller.handle({ type: 'tick', timestamp: clock }) .some(action => action.type === 'voltage_intent'), true)
assert.equal(controller.handle({ type: 'tick', timestamp: clock }) .some(action => action.type === 'voltage_write'), false)

clock += 6000
const highCell = telemetry()
highCell.vmax = 3.585
highCell.vmin = 3.54
const actions = controller.handle({ type: 'telemetry', telemetry: highCell, timestamp: clock })
const intent = actions.find(action => action.type === 'voltage_intent')
assert.ok(intent.command.highCellOverride === 'WARNING')
assert.ok(intent.value <= 55.2)

controller.handle({ type: 'set_mode', value: MODES.ACTIVE, timestamp: clock })
assert.equal(controller.getState().mode, MODES.TEST)
assert.equal(controller.getEvents().some(event => event.type === 'mode_rejected'), true)

clock += 6000
const stale = { ...highCell, timestamp: clock - 10000, telemetryAge: 10000 }
controller.handle({ type: 'telemetry', telemetry: stale, timestamp: clock })
controller.handle({ type: 'set_enabled', value: true, timestamp: clock })
assert.equal(controller.getStatus().telemetry.valid, false)
assert.equal(controller.getStatus().telemetry.ccl, 0)

const configured = validateConfig({ kp: 2, ki: 0.01 })
assert.equal(configured.valid, true)
const thermalCommand = controller.handle({ type: 'telemetry', telemetry: telemetry(), timestamp: clock })
assert.equal(thermalCommand.find(action => action.type === 'voltage_intent').command.chargeCurrentCommand, 0)
