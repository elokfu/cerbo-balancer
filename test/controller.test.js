'use strict'

const assert = require('assert/strict')
const { STATES, CONTROLLER_VERSION, DEFAULT_CONFIG, createBalancerController, validateConfig } = require('../src/controller')

let clock = 1000000

function battery (address = 2, overrides = {}) {
  const cells = Array.from({ length: 16 }, (_, index) => ({ index: index + 1, voltage: 3.40 + index / 1000 }))
  return {
    address,
    valid: true,
    system61Valid: true,
    soc: 99,
    vmin: 3.400,
    vminId: 0x0100 + address,
    vmax: 3.440,
    vmaxId: 0x0700 + address,
    spread: 0.040,
    current: 1.0,
    effectiveCells: cells,
    status44: {
      status1: { active: [] },
      status2: { chargeMosfet: true },
      status3: { effectiveDischarging: false }
    },
    ...overrides
  }
}

function telemetry (batteryValues = [battery()], overrides = {}) {
  return {
    timestamp: clock,
    valid: true,
    cellTelemetryValid: true,
    addressedSystemTelemetryValid: true,
    ccl: 100,
    cvl: 56.5,
    limits: { statusFlags: { chargeEnabled: true, dischargeEnabled: true } },
    effectiveControl: { effectiveChargeCurrent: 100, thermalFactor: 1, statusFlags: { chargeEnabled: true } },
    chargeControlSettings: {
      readbackValid: true,
      maxChargeVoltage: 55.0,
      maxChargeCurrent: 100.0,
      voltageLimitEnabled: true,
      currentLimitEnabled: true
    },
    activeBms: { source: 'virtual', virtualSelected: true, readbackValid: true, label: 'RS485 virtual BMS active', activeBmsInstance: 100 },
    expectedBatteries: batteryValues.map(item => item.address),
    expectedAddresses: batteryValues.map(item => item.address),
    inventory: { pendingRemoval: [] },
    batteries: batteryValues,
    ...overrides
  }
}

function command (actions) { return actions.find(action => action.type === 'controller_command').value }
function sample (controller, batteryValues, overrides = {}) {
  clock += 8000
  return controller.handle({ type: 'telemetry', timestamp: clock, telemetry: telemetry(batteryValues, overrides) })
}

assert.equal(validateConfig({}).valid, true)
assert.equal(validateConfig({ feedForwardAlpha: 1.1 }).valid, false)
assert.equal(validateConfig({ feedForwardGain: -0.1 }).valid, false)
assert.equal(validateConfig({ feedForwardGain: 0 }).valid, true)
assert.equal(validateConfig({ solarDetectionSamples: 3.5 }).valid, false)

const normal = createBalancerController({ now: () => clock })
normal.handle({ type: 'telemetry', telemetry: telemetry(), timestamp: clock })
assert.equal(normal.getState().state, STATES.BALANCING)
assert.equal(normal.getState().selectedAddress, 2)
assert.equal(normal.getState().automaticBalancingEnabled, true)
assert.equal(command(normal.handle({ type: 'tick', timestamp: clock })).mode, undefined)

// NORMAL follows the read-only limits configured in the Cerbo Charge Control UI.
const uiLimits = createBalancerController({ now: () => clock })
uiLimits.handle({ type: 'telemetry', telemetry: telemetry([battery(2, { spread: 0.020 })], {
  chargeControlSettings: {
    readbackValid: true,
    maxChargeVoltage: 54.8,
    maxChargeCurrent: 42,
    voltageLimitEnabled: true,
    currentLimitEnabled: true
  }
}), timestamp: clock })
const uiNormalCommand = command(uiLimits.handle({ type: 'tick', timestamp: clock }))
assert.equal(uiNormalCommand.requestedVoltage, 54.8)
assert.equal(uiNormalCommand.requestedCurrent, 42)

// A slave battery without addressed CID2 61 is selectable from its valid 0x42
// extrema; its SOC falls back to the master's CID2 61 value.
const slaveEligible = createBalancerController({ now: () => clock })
const slave = battery(2, { system61Valid: false, soc: undefined })
slaveEligible.handle({ type: 'telemetry', telemetry: telemetry([slave], { system: { soc61: 85 } }), timestamp: clock })
assert.equal(slaveEligible.getState().state, STATES.BALANCING)
assert.equal(slaveEligible.getState().selectedAddress, 2)
assert.equal(slaveEligible.getStatus().selectedSoc, 85)

// Exactly 30 mV is not eligible; strictly greater is eligible.
const exactThreshold = createBalancerController({ now: () => clock })
exactThreshold.handle({ type: 'telemetry', telemetry: telemetry([battery(2, { spread: 0.030 })]), timestamp: clock })
assert.equal(exactThreshold.getState().state, STATES.NORMAL)

// Selection remains locked even when another battery later has the larger spread.
const lock = createBalancerController({ now: () => clock })
lock.handle({ type: 'telemetry', telemetry: telemetry([battery(2), battery(3, { spread: 0.080 })]), timestamp: clock })
assert.equal(lock.getState().selectedAddress, 2)
sample(lock, [battery(2, { spread: 0.010 }), battery(3, { spread: 0.090 })])
assert.equal(lock.getState().selectedAddress, 2)

// Effective discharge completion is strict below 30 mV and does not latch full SOC.
sample(lock, [battery(2, { soc: 99, spread: 0.030, status44: { status1: { active: [] }, status2: { chargeMosfet: true }, status3: { effectiveDischarging: true } } }), battery(3, { spread: 0.020 })])
assert.equal(lock.getState().state, STATES.BALANCING)
sample(lock, [battery(2, { soc: 99, spread: 0.029, status44: { status1: { active: [] }, status2: { chargeMosfet: true }, status3: { effectiveDischarging: true } } }), battery(3, { spread: 0.020 })])
assert.equal(lock.getState().state, STATES.NORMAL)
assert.equal(lock.getState().completionLatched, false)
assert.equal(lock.getState().lastStopReason, 'BALANCE_DISCHARGE_COMPLETE')

// A selected battery MOSFET interruption reselects the next battery without inhibiting charge.
const reselect = createBalancerController({ now: () => clock })
reselect.handle({ type: 'telemetry', telemetry: telemetry([battery(2), battery(3)]), timestamp: clock })
const off = battery(2, { status44: { status1: { active: [] }, status2: { chargeMosfet: false }, status3: { effectiveDischarging: false } } })
const reselectActions = sample(reselect, [off, battery(3)])
assert.equal(reselect.getState().selectedAddress, 3)
assert.equal(command(reselectActions).chargeEnabled, true)

// All expected integer SOC values must equal 100 before completion latches.
const full = createBalancerController({ now: () => clock })
full.handle({ type: 'telemetry', telemetry: telemetry([battery(2), battery(3)]), timestamp: clock })
sample(full, [battery(2, { soc: 100 }), battery(3, { soc: 99 })])
assert.equal(full.getState().state, STATES.BALANCING)
sample(full, [battery(2, { soc: 100 }), battery(3, { soc: 100 })])
assert.equal(full.getState().state, STATES.NORMAL)
assert.equal(full.getState().completionLatched, true)

// Completion uses the master SOC fallback when slaves have no addressed SOC.
const masterSocCompletion = createBalancerController({ now: () => clock })
masterSocCompletion.handle({ type: 'telemetry', telemetry: telemetry([battery(2, { soc: undefined }), battery(3, { soc: undefined })], { system: { soc61: 99 } }), timestamp: clock })
assert.equal(masterSocCompletion.getState().state, STATES.BALANCING)
sample(masterSocCompletion, [battery(2, { soc: undefined }), battery(3, { soc: undefined })], { system: { soc61: 100 } })
assert.equal(masterSocCompletion.getState().state, STATES.NORMAL)
assert.equal(masterSocCompletion.getState().completionLatched, true)
assert.equal(masterSocCompletion.getState().lastStopReason, 'FULL_SOC_COMPLETE')
sample(full, [battery(2, { soc: 99 }), battery(3, { soc: 99 })], { limits: { statusFlags: { chargeEnabled: false, dischargeEnabled: true } } })
assert.equal(full.getState().permissionOffSeen, true)
for (let index = 0; index < 2; index++) sample(full, [battery(2), battery(3)])
assert.equal(full.getState().completionLatched, false)

// Safety fallback is charge-capable and uses 55 V / 10 A.
const safety = createBalancerController({ now: () => clock })
const safetyCommand = command(safety.handle({ type: 'tick', timestamp: clock }))
assert.equal(safety.getState().state, STATES.SAFETY_STOP)
assert.equal(safetyCommand.requestedVoltage, 55)
assert.equal(safetyCommand.requestedCurrent, 10)
assert.equal(safetyCommand.chargeEnabled, true)

// Build a high held request, then verify four true deficits pause without changing state.
const solar = createBalancerController({ now: () => clock })
solar.handle({ type: 'telemetry', telemetry: telemetry([battery(2, { current: 1 }), battery(3, { current: 9 })]), timestamp: clock })
solar.handle({ type: 'configure', config: { ...solar.getConfig(), currentIncreaseRatePerMin: 600 }, timestamp: clock })
for (let index = 0; index < 3; index++) sample(solar, [battery(2, { current: 1 }), battery(3, { current: 9 })])
assert.ok(solar.getState().aggregateCurrentCommand > 4)
sample(solar, [battery(2, { current: 2 }), battery(3, { current: solar.getState().aggregateCurrentCommand })])
for (let index = 0; index < 3; index++) sample(solar, [battery(2, { current: 1 }), battery(3, { current: 0 })])
assert.equal(solar.getState().solarLimited, false)
const pauseActions = sample(solar, [battery(2, { current: 1 }), battery(3, { current: 0 })])
assert.equal(solar.getState().solarLimited, true)
assert.equal(solar.getState().state, STATES.BALANCING)
const held = solar.getState().aggregateCurrentCommand
assert.equal(command(pauseActions).reason, 'SOLAR_LIMITED_PAUSE')
for (let index = 0; index < 4; index++) sample(solar, [battery(2, { current: 2 }), battery(3, { current: Math.max(0, held - 2) })])
assert.equal(solar.getState().solarLimited, false)

// A BMS cap must freeze PI without being classified as a solar limitation.
const limited = createBalancerController({ now: () => clock })
limited.handle({ type: 'telemetry', telemetry: telemetry([battery(2, { current: 1 })], { ccl: 1, effectiveControl: { effectiveChargeCurrent: 1, thermalFactor: 1 } }), timestamp: clock })
for (let index = 0; index < 5; index++) sample(limited, [battery(2, { current: 1 })], { ccl: 1, effectiveControl: { effectiveChargeCurrent: 1, thermalFactor: 1 } })
assert.equal(limited.getState().solarLimited, false)

// Cerbo selection is reflected as controller authority without a mode switch.
const canSelected = createBalancerController({ now: () => clock })
canSelected.handle({ type: 'telemetry', telemetry: telemetry([battery()], { activeBms: { source: 'can', virtualSelected: false, readbackValid: true, activeBmsInstance: 512 } }), timestamp: clock })
assert.equal(canSelected.getStatus().authorityState, 'SHADOW')
assert.equal(canSelected.getStatus().mode, undefined)

// Automatic balancing is ON for a fresh or reset controller and OFF persists.
const automatic = createBalancerController({ now: () => clock })
assert.equal(automatic.getState().automaticBalancingEnabled, true)
automatic.handle({ type: 'telemetry', telemetry: telemetry(), timestamp: clock })
assert.equal(automatic.getState().state, STATES.BALANCING)
const automaticOff = automatic.handle({ type: 'set_automatic_balancing', value: false, timestamp: clock })
assert.equal(automatic.getState().state, STATES.NORMAL)
assert.equal(automatic.getState().selectedAddress, null)
assert.equal(automatic.getState().automaticBalancingEnabled, false)
assert.equal(command(automaticOff).reason, 'AUTOMATIC_BALANCING_OFF')
assert.equal(command(automaticOff).requestedVoltage, 55.0)
assert.equal(command(automaticOff).requestedCurrent, 100.0)
sample(automatic, [battery(2, { spread: 0.090 })])
assert.equal(automatic.getState().state, STATES.NORMAL)
assert.equal(automatic.getState().selectedAddress, null)

const restoredOff = createBalancerController({ now: () => clock })
restoredOff.handle({
  type: 'load',
  state: { version: CONTROLLER_VERSION, state: STATES.NORMAL, automaticBalancingEnabled: false },
  config: {},
  timestamp: clock
})
restoredOff.handle({ type: 'telemetry', telemetry: telemetry(), timestamp: clock })
assert.equal(restoredOff.getState().automaticBalancingEnabled, false)
assert.equal(restoredOff.getState().state, STATES.NORMAL)

// Version 3 enabled state migrates; missing enable state defaults ON.
const migratedOff = createBalancerController({ now: () => clock })
migratedOff.handle({ type: 'load', state: { version: 3, state: STATES.NORMAL, mode: 'TEST', enabled: false }, config: {}, timestamp: clock })
assert.equal(migratedOff.getState().automaticBalancingEnabled, false)
const migratedDefault = createBalancerController({ now: () => clock })
migratedDefault.handle({ type: 'load', state: { version: 3, state: STATES.NORMAL, mode: 'TEST' }, config: {}, timestamp: clock })
assert.equal(migratedDefault.getState().automaticBalancingEnabled, true)

// Reset control preserves the switch; restore defaults turns it ON.
restoredOff.handle({ type: 'reset_integrator', timestamp: clock })
assert.equal(restoredOff.getState().automaticBalancingEnabled, false)
restoredOff.handle({ type: 'restore_defaults', timestamp: clock })
assert.equal(restoredOff.getState().automaticBalancingEnabled, true)

// Version 4's exact legacy Ki is upgraded; custom Ki remains unchanged.
const migratedKi = createBalancerController({ now: () => clock })
migratedKi.handle({ type: 'load', state: { version: 4, state: STATES.NORMAL }, config: { ki: 0.002 }, timestamp: clock })
assert.equal(migratedKi.getConfig().ki, 0.02)
const customKi = createBalancerController({ now: () => clock })
customKi.handle({ type: 'load', state: { version: 4, state: STATES.NORMAL }, config: { ki: 0.015 }, timestamp: clock })
assert.equal(customKi.getConfig().ki, 0.015)

// Configuration requests return a matching acceptance/rejection result.
let configActions = customKi.handle({ type: 'configure', requestId: 'good', config: { ...customKi.getConfig(), ki: 0.1 }, timestamp: clock })
assert.deepEqual(configActions.find(action => action.type === 'status').status.configurationResult, { requestId: 'good', accepted: true, errors: [] })
configActions = customKi.handle({ type: 'configure', requestId: 'bad', config: { ...customKi.getConfig(), ki: 0 }, timestamp: clock })
assert.equal(configActions.find(action => action.type === 'status').status.configurationResult.accepted, false)
assert.equal(customKi.getConfig().ki, 0.1)

// Feed-forward gain zero retains the 2 A startup request and removes dynamic contribution.
const piOnly = createBalancerController({ now: () => clock })
piOnly.handle({ type: 'configure', config: { ...piOnly.getConfig(), feedForwardGain: 0, ki: 0.1 }, timestamp: clock })
let piActions = piOnly.handle({ type: 'telemetry', telemetry: telemetry([battery(2, { current: 0.5 })]), timestamp: clock })
assert.equal(command(piActions).requestedCurrent, DEFAULT_CONFIG.feedForwardFallbackCurrent)
assert.equal(command(piActions).effectiveFeedForwardCurrent, 0)
piActions = sample(piOnly, [battery(2, { current: 0.5 })])
assert.equal(command(piActions).effectiveFeedForwardCurrent, 0)
assert.ok(piOnly.getState().aggregateCurrentCommand <= 2 + 10 * 8 / 60 + 1e-9)
