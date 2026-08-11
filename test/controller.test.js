'use strict'

const assert = require('assert/strict')
const { STATES, MODES, createBalancerController, validateConfig } = require('../src/controller')

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
    activeBms: { source: 'virtual', virtualSelected: true, label: 'RS485 virtual BMS active', activeBmsInstance: 100 },
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
assert.equal(validateConfig({ solarDetectionSamples: 3.5 }).valid, false)

const normal = createBalancerController({ now: () => clock })
normal.handle({ type: 'telemetry', telemetry: telemetry(), timestamp: clock })
normal.handle({ type: 'set_enabled', value: true, timestamp: clock })
assert.equal(normal.getState().state, STATES.BALANCING)
assert.equal(normal.getState().selectedAddress, 2)
assert.equal(command(normal.handle({ type: 'tick', timestamp: clock })).mode, MODES.TEST)

// CID2 42 cells cannot make an addressed-CID2 61 ineligible battery selectable.
const sourceAuthority = createBalancerController({ now: () => clock })
const wideCells = battery(2, { spread: 0.020, effectiveCells: [{ voltage: 2.5 }, ...Array.from({ length: 15 }, () => ({ voltage: 4.0 }))] })
sourceAuthority.handle({ type: 'telemetry', telemetry: telemetry([wideCells]), timestamp: clock })
sourceAuthority.handle({ type: 'set_enabled', value: true, timestamp: clock })
assert.equal(sourceAuthority.getState().state, STATES.NORMAL)

// Exactly 30 mV is not eligible; strictly greater is eligible.
const exactThreshold = createBalancerController({ now: () => clock })
exactThreshold.handle({ type: 'telemetry', telemetry: telemetry([battery(2, { spread: 0.030 })]), timestamp: clock })
exactThreshold.handle({ type: 'set_enabled', value: true, timestamp: clock })
assert.equal(exactThreshold.getState().state, STATES.NORMAL)

// Selection remains locked even when another battery later has the larger spread.
const lock = createBalancerController({ now: () => clock })
lock.handle({ type: 'telemetry', telemetry: telemetry([battery(2), battery(3, { spread: 0.080 })]), timestamp: clock })
lock.handle({ type: 'set_enabled', value: true, timestamp: clock })
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
reselect.handle({ type: 'set_enabled', value: true, timestamp: clock })
const off = battery(2, { status44: { status1: { active: [] }, status2: { chargeMosfet: false }, status3: { effectiveDischarging: false } } })
const reselectActions = sample(reselect, [off, battery(3)])
assert.equal(reselect.getState().selectedAddress, 3)
assert.equal(command(reselectActions).chargeEnabled, true)

// All expected integer SOC values must equal 100 before completion latches.
const full = createBalancerController({ now: () => clock })
full.handle({ type: 'telemetry', telemetry: telemetry([battery(2), battery(3)]), timestamp: clock })
full.handle({ type: 'set_enabled', value: true, timestamp: clock })
sample(full, [battery(2, { soc: 100 }), battery(3, { soc: 99 })])
assert.equal(full.getState().state, STATES.BALANCING)
sample(full, [battery(2, { soc: 100 }), battery(3, { soc: 100 })])
assert.equal(full.getState().state, STATES.NORMAL)
assert.equal(full.getState().completionLatched, true)
sample(full, [battery(2, { soc: 99 }), battery(3, { soc: 99 })], { limits: { statusFlags: { chargeEnabled: false, dischargeEnabled: true } } })
assert.equal(full.getState().permissionOffSeen, true)
for (let index = 0; index < 2; index++) sample(full, [battery(2), battery(3)])
assert.equal(full.getState().completionLatched, false)

// Safety fallback is charge-capable and uses 55 V / 10 A.
const safety = createBalancerController({ now: () => clock })
safety.handle({ type: 'set_enabled', value: true, timestamp: clock })
const safetyCommand = command(safety.handle({ type: 'tick', timestamp: clock }))
assert.equal(safety.getState().state, STATES.SAFETY_STOP)
assert.equal(safetyCommand.requestedVoltage, 55)
assert.equal(safetyCommand.requestedCurrent, 10)
assert.equal(safetyCommand.chargeEnabled, true)

// Build a high held request, then verify four true deficits pause without changing state.
const solar = createBalancerController({ now: () => clock })
solar.handle({ type: 'telemetry', telemetry: telemetry([battery(2, { current: 1 }), battery(3, { current: 9 })]), timestamp: clock })
solar.handle({ type: 'configure', config: { ...solar.getConfig(), currentIncreaseRatePerMin: 600 }, timestamp: clock })
solar.handle({ type: 'set_enabled', value: true, timestamp: clock })
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
limited.handle({ type: 'set_enabled', value: true, timestamp: clock })
for (let index = 0; index < 5; index++) sample(limited, [battery(2, { current: 1 })], { ccl: 1, effectiveControl: { effectiveChargeCurrent: 1, thermalFactor: 1 } })
assert.equal(limited.getState().solarLimited, false)

// ACTIVE remains a separate manually selected and read-back-gated mode.
const canSelected = createBalancerController({ now: () => clock })
canSelected.handle({ type: 'telemetry', telemetry: telemetry([battery()], { activeBms: { source: 'can', virtualSelected: false, activeBmsInstance: 512 } }), timestamp: clock })
canSelected.handle({ type: 'set_output_ready', value: true, timestamp: clock })
canSelected.handle({ type: 'set_mode', value: MODES.ACTIVE, timestamp: clock })
assert.equal(canSelected.getStatus().mode, MODES.TEST)
