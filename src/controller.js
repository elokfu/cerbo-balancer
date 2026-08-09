'use strict'

const STATES = Object.freeze({
  NORMAL: 'NORMAL',
  BALANCE_ELIGIBILITY: 'BALANCE_ELIGIBILITY',
  BALANCE_RESTART: 'BALANCE_RESTART',
  BALANCE_CURRENT_CONTROL: 'BALANCE_CURRENT_CONTROL',
  BALANCE_CONFIRM: 'BALANCE_CONFIRM',
  BALANCE_DISCHARGE_RECOVERY: 'BALANCE_DISCHARGE_RECOVERY',
  PROTECTION_RECOVERY: 'PROTECTION_RECOVERY',
  FAULT_OR_ABORT: 'FAULT_OR_ABORT'
})

const MODES = Object.freeze({ TEST: 'TEST', ACTIVE: 'ACTIVE' })

const DEFAULT_CONFIG = Object.freeze({
  normalPackVoltageMax: 55.2,
  balancingVoltageCeiling: 56.5,
  protectionRecoveryVoltage: 54.0,
  socEntryThreshold: 98,
  socExitThreshold: 97,
  balancerSpreadThreshold: 0.035,
  balanceBatteryCurrentTarget: 2.0,
  balancerMinimumCurrent: 1.5,
  selectedVmaxStop: 3.550,
  completionCellMinimum: 3.450,
  finalGlobalSpread: 0.010,
  confirmationMs: 15 * 60 * 1000,
  warningCellVoltage: 3.580,
  softwareEmergencyCellVoltage: 3.590,
  dynessMaximumCellVoltage: 3.600,
  // Six-second RS485 cycles need room for three batteries and bounded
  // diagnostics. This allows a complete cycle plus one delayed retry while
  // still failing safe on a genuine communication loss.
  telemetryStaleMs: 15000,
  protectionClearDebounceMs: 5000,
  permissionRestartDebounceMs: 5000,
  currentTargetHoldMs: 10000,
  currentTargetTolerance: 0.25,
  aggregateCurrentMinimum: 0.0,
  aggregateCurrentMaximum: 100.0,
  kp: 0.5,
  ki: 0.01,
  maxIntegralTerm: 100,
  currentIncreaseRatePerMin: 10,
  maximumDischargeRecoveryMs: 4 * 60 * 60 * 1000,
  maximumAutomaticBalancingCycles: 10,
  cycleWindowMs: 24 * 60 * 60 * 1000
})

function finite (value) { return typeof value === 'number' && Number.isFinite(value) }
function bool (value) { return value === true }

function emptyState () {
  return {
    version: 2, state: STATES.NORMAL, mode: MODES.TEST, enabled: false,
    autoManualMode: 'AUTO', selectedAddress: null, selectedReason: null,
    cycleCount: 0, cycleWindowStartedAt: null, recoveryStartedAt: null,
    permissionStartedAt: null, targetHoldStartedAt: null,
    confirmationStartedAt: null, protectionClearStartedAt: null,
    integralTerm: 0, aggregateCurrentCommand: 0, lastEvaluationAt: null,
    lastControlSampleTimestamp: null,
    lastTransitionAt: null, lastStopReason: null, fault: null, lastEvent: null,
    csvLogging: { enabled: false, filename: null }
  }
}

function validateConfig (candidate = {}) {
  const config = { ...DEFAULT_CONFIG, ...candidate }
  // Never let a persisted legacy setting turn normal six-second cycles into
  // false stale-telemetry faults.
  config.telemetryStaleMs = Math.max(DEFAULT_CONFIG.telemetryStaleMs, config.telemetryStaleMs)
  const errors = []
  const positive = ['normalPackVoltageMax', 'balancingVoltageCeiling', 'protectionRecoveryVoltage',
    'socEntryThreshold', 'socExitThreshold', 'balancerSpreadThreshold', 'balanceBatteryCurrentTarget',
    'balancerMinimumCurrent', 'selectedVmaxStop', 'completionCellMinimum', 'finalGlobalSpread',
    'confirmationMs', 'warningCellVoltage', 'softwareEmergencyCellVoltage', 'dynessMaximumCellVoltage',
    'telemetryStaleMs', 'protectionClearDebounceMs', 'permissionRestartDebounceMs', 'currentTargetHoldMs',
    'currentTargetTolerance', 'aggregateCurrentMaximum', 'kp', 'ki', 'maxIntegralTerm',
    'currentIncreaseRatePerMin', 'maximumDischargeRecoveryMs', 'maximumAutomaticBalancingCycles', 'cycleWindowMs']
  for (const field of positive) if (!finite(config[field]) || config[field] <= 0) errors.push(`${field} must be positive`)
  if (!finite(config.aggregateCurrentMinimum) || config.aggregateCurrentMinimum < 0) errors.push('aggregateCurrentMinimum must be non-negative')
  if (!(config.socExitThreshold < config.socEntryThreshold)) errors.push('SOC exit threshold must be below entry threshold')
  if (!(config.balancerMinimumCurrent < config.balanceBatteryCurrentTarget)) errors.push('minimum current must be below target current')
  if (!(config.finalGlobalSpread < config.balancerSpreadThreshold)) errors.push('final spread must be below start spread')
  if (!(config.completionCellMinimum < config.selectedVmaxStop && config.selectedVmaxStop < config.warningCellVoltage && config.warningCellVoltage < config.softwareEmergencyCellVoltage && config.softwareEmergencyCellVoltage < config.dynessMaximumCellVoltage)) errors.push('cell voltage thresholds are invalid')
  if (!(config.aggregateCurrentMinimum <= config.aggregateCurrentMaximum)) errors.push('aggregate current bounds are invalid')
  return { valid: errors.length === 0, errors, config }
}

function createBalancerController (options = {}) {
  const now = options.now || (() => Date.now())
  const localDate = options.localDate || (timestamp => new Date(timestamp).toISOString().slice(0, 10))
  let config = { ...DEFAULT_CONFIG }
  let state = emptyState()
  let telemetry = null
  let outputReady = false
  const events = []
  const history = []
  let lastPersistenceKey = null
  let pendingCsvLogging = null

  function record (timestamp, type, detail, extra = {}) {
    const event = { timestamp, date: localDate(timestamp), type, detail, ...extra }
    events.push(event); state.lastEvent = event
    return event
  }
  function transition (next, timestamp, reason) {
    if (state.state === next) return
    record(timestamp, 'state_transition', `${state.state} -> ${next}: ${reason}`)
    state.state = next; state.lastTransitionAt = timestamp
  }
  function resetPi () { state.integralTerm = 0; state.aggregateCurrentCommand = config.aggregateCurrentMinimum; state.targetHoldStartedAt = null; state.lastControlSampleTimestamp = null }
  function releaseSelection () { state.selectedAddress = null; state.selectedReason = null; state.recoveryStartedAt = null; state.permissionStartedAt = null; state.confirmationStartedAt = null }
  function freshTelemetry (timestamp) {
    const age = telemetry && finite(telemetry.timestamp) ? Math.max(0, timestamp - telemetry.timestamp) : Infinity
    const expected = telemetry && Array.isArray(telemetry.expectedBatteries) ? telemetry.expectedBatteries : []
    const batteries = telemetry && Array.isArray(telemetry.batteries) ? telemetry.batteries : []
    const complete = batteries.length > 0 && batteries.every(b => b.valid === true && Array.isArray(b.effectiveCells || b.cells) && (b.effectiveCells || b.cells).length === 16)
    const expectedComplete = !expected.length || expected.every(address => batteries.some(b => b.address === address && b.valid === true))
    const valid = Boolean(telemetry && telemetry.valid && telemetry.cellTelemetryValid && age <= config.telemetryStaleMs && complete && expectedComplete)
    return { valid, age, lockout: valid ? null : 'complete fresh telemetry from every expected battery is required' }
  }
  function selectedBattery () { return telemetry && (telemetry.batteries || []).find(b => b.address === state.selectedAddress) }
  function limitFlags () { return telemetry && telemetry.limits && telemetry.limits.statusFlags ? telemetry.limits.statusFlags : (telemetry && telemetry.statusFlags) || {} }
  function chargePermitted () { return bool(limitFlags().chargeEnabled) }
  function ccl () { return telemetry && finite(telemetry.ccl) ? Math.max(0, telemetry.ccl) : 0 }
  function hasProtection () {
    if (!telemetry) return true
    if (telemetry.hardProtection === true) return true
    return (telemetry.batteries || []).some(b => b.status44 && b.status44.status1 && Array.isArray(b.status44.status1.active) && b.status44.status1.active.length)
  }
  function eligible (battery, entry) {
    const cells = battery && (battery.effectiveCells || battery.cells)
    const spread = battery && finite(battery.cellSpread) ? battery.cellSpread : cells && cells.length ? Math.max(...cells.map(c => finite(c.voltage) ? c.voltage : c)) - Math.min(...cells.map(c => finite(c.voltage) ? c.voltage : c)) : NaN
    const mosfet = battery && battery.status44 && battery.status44.status2 ? battery.status44.status2.chargeMosfet : battery && battery.chargeMosfet
    return Boolean(battery && battery.valid && Array.isArray(cells) && cells.length === 16 && spread > config.balancerSpreadThreshold && chargePermitted() && ccl() > 0 && !hasProtection() && mosfet === true && (!entry || (finite(telemetry.soc) && telemetry.soc > config.socEntryThreshold)))
  }
  function selectedTelemetryHealthy (battery) {
    const cells = battery && (battery.effectiveCells || battery.cells)
    return Boolean(battery && battery.valid && Array.isArray(cells) && cells.length === 16 && !hasProtection())
  }
  function globalVmax () { return telemetry && finite(telemetry.vmax) ? telemetry.vmax : -Infinity }
  function globalVmin () { return telemetry && finite(telemetry.vmin) ? telemetry.vmin : Infinity }
  function selectedVmax (battery) {
    const cells = battery && (battery.effectiveCells || battery.cells)
    return cells && cells.length ? Math.max(...cells.map(c => finite(c.voltage) ? c.voltage : c)) : -Infinity
  }
  function safeNormalCommand (reason) { return { requestedVoltage: config.normalPackVoltageMax, requestedCurrent: 100, chargeEnabled: true, reason } }
  function disabledCommand (reason, voltage = config.balancingVoltageCeiling) { return { requestedVoltage: voltage, requestedCurrent: 0, chargeEnabled: false, reason } }
  function activeCurrentCommand (timestamp, battery) {
    const sampleTimestamp = telemetry && finite(telemetry.timestamp) ? telemetry.timestamp : timestamp
    const error = config.balanceBatteryCurrentTarget - (finite(battery.current) ? battery.current : 0)
    const previousSample = state.lastControlSampleTimestamp
    if (previousSample === sampleTimestamp) {
      const effective = Math.min(state.aggregateCurrentCommand, ccl())
      return { requestedVoltage: config.balancingVoltageCeiling, requestedCurrent: effective, chargeEnabled: true, reason: 'BALANCE_CURRENT_CONTROL', pTerm: config.kp * error, iTerm: state.integralTerm, currentError: error, outputSaturated: effective < state.aggregateCurrentCommand }
    }
    const dt = previousSample === null ? 0 : Math.max(0, (sampleTimestamp - previousSample) / 1000)
    const pTerm = config.kp * error
    let iTerm = state.integralTerm
    if (dt > 0) iTerm = Math.max(-config.maxIntegralTerm, Math.min(config.maxIntegralTerm, iTerm + config.ki * error * dt))
    const desired = Math.max(config.aggregateCurrentMinimum, Math.min(config.aggregateCurrentMaximum, state.aggregateCurrentCommand + pTerm + iTerm))
    const rise = config.currentIncreaseRatePerMin * dt / 60
    const command = desired > state.aggregateCurrentCommand ? Math.min(desired, state.aggregateCurrentCommand + rise) : desired
    const effective = Math.min(command, ccl())
    const saturated = effective < command || effective < desired
    if (saturated && error > 0) iTerm = state.integralTerm
    state.integralTerm = iTerm; state.aggregateCurrentCommand = command; state.lastControlSampleTimestamp = sampleTimestamp
    return { requestedVoltage: config.balancingVoltageCeiling, requestedCurrent: effective, chargeEnabled: true, reason: 'BALANCE_CURRENT_CONTROL', pTerm, iTerm, currentError: error, outputSaturated: saturated }
  }
  function startRecovery (timestamp, reason) {
    state.lastStopReason = reason; resetPi(); state.recoveryStartedAt = timestamp; state.permissionStartedAt = null
    transition(STATES.BALANCE_DISCHARGE_RECOVERY, timestamp, reason)
    record(timestamp, reason, 'charging disabled for natural discharge recovery', { selectedAddress: state.selectedAddress })
  }
  function socExit (timestamp) {
    state.lastStopReason = 'SOC_EXIT'; resetPi(); releaseSelection(); transition(STATES.NORMAL, timestamp, 'SOC is at or below exit threshold')
    record(timestamp, 'SOC_EXIT', 'selected sequence ended at SOC exit threshold')
  }
  function evaluate (timestamp) {
    const actions = []
    const info = freshTelemetry(timestamp)
    let command = safeNormalCommand('NORMAL_POLICY')
    if (!state.enabled) {
      if (state.state !== STATES.NORMAL) transition(STATES.NORMAL, timestamp, 'controller disabled')
      resetPi(); releaseSelection()
      command = safeNormalCommand('CONTROLLER_DISABLED')
    } else if (!info.valid) {
      const balancingState = [STATES.BALANCE_ELIGIBILITY, STATES.BALANCE_RESTART, STATES.BALANCE_CURRENT_CONTROL, STATES.BALANCE_CONFIRM, STATES.BALANCE_DISCHARGE_RECOVERY, STATES.PROTECTION_RECOVERY].includes(state.state)
      if (balancingState || state.mode === MODES.ACTIVE) {
        state.fault = info.lockout; resetPi(); transition(STATES.FAULT_OR_ABORT, timestamp, info.lockout)
      }
      command = disabledCommand('TELEMETRY_LOCKOUT', config.protectionRecoveryVoltage)
    } else if (state.state === STATES.FAULT_OR_ABORT && state.mode === MODES.TEST && state.fault === 'complete fresh telemetry from every expected battery is required') {
      state.fault = null; resetPi(); releaseSelection(); transition(STATES.NORMAL, timestamp, 'TEST telemetry recovered')
      command = safeNormalCommand('TEST_TELEMETRY_RECOVERED')
    } else if (state.state === STATES.FAULT_OR_ABORT) {
      command = disabledCommand('FAULT_OR_ABORT', config.protectionRecoveryVoltage)
    } else {
      const battery = selectedBattery()
      const soc = telemetry.soc
      const hard = hasProtection() || globalVmax() >= config.dynessMaximumCellVoltage
      const emergency = globalVmax() >= config.softwareEmergencyCellVoltage
      const warning = globalVmax() >= config.warningCellVoltage
      const activeStates = [STATES.BALANCE_ELIGIBILITY, STATES.BALANCE_RESTART, STATES.BALANCE_CURRENT_CONTROL, STATES.BALANCE_CONFIRM, STATES.BALANCE_DISCHARGE_RECOVERY, STATES.PROTECTION_RECOVERY]
      if (activeStates.includes(state.state) && (!finite(soc) || soc <= config.socExitThreshold)) {
        socExit(timestamp); command = safeNormalCommand('SOC_EXIT')
      } else if (hard || emergency) {
        resetPi(); state.protectionClearStartedAt = null; transition(STATES.PROTECTION_RECOVERY, timestamp, hard ? 'hard BMS/global cell protection' : 'software emergency')
        command = disabledCommand('PROTECTION_RECOVERY', config.protectionRecoveryVoltage)
      } else if (state.state === STATES.PROTECTION_RECOVERY) {
        command = disabledCommand('PROTECTION_RECOVERY', config.protectionRecoveryVoltage)
        if (!hasProtection() && globalVmax() < config.warningCellVoltage && chargePermitted() && ccl() > 0 && battery && eligible(battery, false)) {
          state.protectionClearStartedAt = state.protectionClearStartedAt || timestamp
          if (timestamp - state.protectionClearStartedAt >= config.protectionClearDebounceMs) transition(STATES.BALANCE_RESTART, timestamp, 'protection clear debounce complete')
        } else state.protectionClearStartedAt = null
      } else if (state.state === STATES.NORMAL) {
        const candidate = state.autoManualMode === 'MANUAL' && state.selectedAddress ? (telemetry.batteries || []).find(b => b.address === state.selectedAddress) : (telemetry.batteries || []).find(b => eligible(b, true))
        if (candidate && eligible(candidate, true)) {
          state.selectedAddress = candidate.address; state.selectedReason = 'first eligible configured battery'; state.cycleWindowStartedAt = state.cycleWindowStartedAt || timestamp
          transition(STATES.BALANCE_ELIGIBILITY, timestamp, `battery ${candidate.address} selected`)
          command = disabledCommand('BALANCE_ELIGIBILITY')
        }
      } else if (!selectedTelemetryHealthy(battery)) {
        state.fault = 'selected battery is invalid, protected, unavailable, or no longer charge-capable'; resetPi(); transition(STATES.FAULT_OR_ABORT, timestamp, state.fault); command = disabledCommand('SELECTED_BATTERY_FAULT', config.protectionRecoveryVoltage)
      } else if (state.state === STATES.BALANCE_ELIGIBILITY) {
        if (!outputReady) { state.fault = 'output readback is not verified'; transition(STATES.FAULT_OR_ABORT, timestamp, state.fault); command = disabledCommand('OUTPUT_NOT_READY', config.protectionRecoveryVoltage) } else { transition(STATES.BALANCE_RESTART, timestamp, 'eligibility validated'); command = { requestedVoltage: config.balancingVoltageCeiling, requestedCurrent: config.aggregateCurrentMinimum, chargeEnabled: true, reason: 'BALANCE_RESTART' } }
      } else if (state.state === STATES.BALANCE_RESTART || state.state === STATES.BALANCE_CURRENT_CONTROL || state.state === STATES.BALANCE_CONFIRM) {
        if (!chargePermitted()) { startRecovery(timestamp, 'CHARGE_PERMISSION_LOST'); command = disabledCommand('CHARGE_PERMISSION_LOST') } else if (ccl() === 0) { startRecovery(timestamp, 'CCL_ZERO_STOP'); command = disabledCommand('CCL_ZERO_STOP') } else if (selectedVmax(battery) >= config.selectedVmaxStop) { startRecovery(timestamp, 'VMAX_NORMAL_STOP'); command = disabledCommand('VMAX_NORMAL_STOP') } else if ((battery.status44 && battery.status44.status2 && battery.status44.status2.chargeMosfet === false) || battery.chargeMosfet === false) { startRecovery(timestamp, 'CHARGER_MOSFET_OFF'); command = disabledCommand('CHARGER_MOSFET_OFF') } else {
          command = activeCurrentCommand(timestamp, battery)
          if (warning) { command.requestedCurrent = Math.min(command.requestedCurrent, config.aggregateCurrentMinimum); command.reason = 'HIGH_CELL_WARNING' }
          const complete = globalVmin() > config.completionCellMinimum && globalVmax() - globalVmin() < config.finalGlobalSpread
          if (complete) { state.confirmationStartedAt = state.confirmationStartedAt || timestamp; transition(STATES.BALANCE_CONFIRM, timestamp, 'completion criteria met') } else if (state.state === STATES.BALANCE_CONFIRM) { state.confirmationStartedAt = null; transition(STATES.BALANCE_CURRENT_CONTROL, timestamp, 'completion criteria lost') }
          if (state.state === STATES.BALANCE_RESTART) {
            const near = Math.abs((battery.current || 0) - config.balanceBatteryCurrentTarget) <= config.currentTargetTolerance
            state.targetHoldStartedAt = near ? (state.targetHoldStartedAt || timestamp) : null
            if (state.targetHoldStartedAt && timestamp - state.targetHoldStartedAt >= config.currentTargetHoldMs) transition(STATES.BALANCE_CURRENT_CONTROL, timestamp, 'selected current target held')
          }
          if (state.state === STATES.BALANCE_CONFIRM && state.confirmationStartedAt && timestamp - state.confirmationStartedAt >= config.confirmationMs) { record(timestamp, 'BALANCE_COMPLETE', 'confirmation complete'); resetPi(); releaseSelection(); transition(STATES.NORMAL, timestamp, 'balance confirmation complete'); command = safeNormalCommand('BALANCE_COMPLETE') }
        }
      } else if (state.state === STATES.BALANCE_DISCHARGE_RECOVERY) {
        command = disabledCommand('BALANCE_DISCHARGE_RECOVERY')
        if (timestamp - (state.recoveryStartedAt || timestamp) >= config.maximumDischargeRecoveryMs) { state.fault = 'discharge recovery timeout'; transition(STATES.FAULT_OR_ABORT, timestamp, state.fault); command = disabledCommand('RECOVERY_TIMEOUT', config.protectionRecoveryVoltage) } else if (chargePermitted() && ccl() > 0) {
          state.permissionStartedAt = state.permissionStartedAt || timestamp
          if (timestamp - state.permissionStartedAt >= config.permissionRestartDebounceMs) {
            if (!state.cycleWindowStartedAt || timestamp - state.cycleWindowStartedAt > config.cycleWindowMs) { state.cycleWindowStartedAt = timestamp; state.cycleCount = 0 }
            state.cycleCount += 1
            if (state.cycleCount > config.maximumAutomaticBalancingCycles) { state.fault = 'maximum automatic balancing cycles reached'; transition(STATES.FAULT_OR_ABORT, timestamp, state.fault); command = disabledCommand('CYCLE_LIMIT', config.protectionRecoveryVoltage) } else { resetPi(); transition(STATES.BALANCE_RESTART, timestamp, 'charge permission and CCL restart debounce complete') }
          }
        } else state.permissionStartedAt = null
      }
    }
    const controllerCommand = { version: timestamp, timestamp, mode: state.mode, ...command }
    const selected = selectedBattery()
    history.push({
      timestamp,
      batteryCurrent: telemetry && finite(telemetry.packCurrent) ? telemetry.packCurrent : null,
      selectedCurrent: selected && finite(selected.current) ? selected.current : null,
      selectedVmax: selected ? selectedVmax(selected) : null,
      selectedVmin: selected && (selected.effectiveCells || selected.cells) ? Math.min(...(selected.effectiveCells || selected.cells).map(cell => finite(cell.voltage) ? cell.voltage : cell)) : null,
      globalVmax: finite(globalVmax()) ? globalVmax() : null,
      globalVmin: finite(globalVmin()) ? globalVmin() : null,
      globalSpread: finite(globalVmax()) && finite(globalVmin()) ? globalVmax() - globalVmin() : null,
      selectedSpread: selected && finite(selected.cellSpread) ? selected.cellSpread : null,
      ccl: ccl(),
      aggregateCurrentCommand: state.aggregateCurrentCommand,
      chargeEnabled: command.chargeEnabled === true
    })
    if (history.length > 360) history.splice(0, history.length - 360)
    const persistedState = { ...state }
    delete persistedState.lastEvaluationAt
    const persistence = { state: persistedState, config: { ...config } }
    const persistenceKey = JSON.stringify(persistence)
    state.lastEvaluationAt = timestamp
    actions.push({ type: 'controller_command', value: controllerCommand })
    if (pendingCsvLogging) { actions.push({ type: 'csv_logging', value: pendingCsvLogging }); pendingCsvLogging = null }
    if (persistenceKey !== lastPersistenceKey) {
      lastPersistenceKey = persistenceKey
      actions.push({ type: 'persist', ...persistence })
    }
    actions.push({ type: 'status', status: status(timestamp, info, controllerCommand) })
    return actions
  }
  function status (timestamp, info = freshTelemetry(timestamp), command = null) {
    const selected = selectedBattery(); const spread = selected && finite(selected.cellSpread) ? selected.cellSpread : null
    return { state: state.state, mode: state.mode, enabled: state.enabled, autoManualMode: state.autoManualMode, selectedAddress: state.selectedAddress, selectedReason: state.selectedReason, cycleCount: state.cycleCount, lastStopReason: state.lastStopReason, fault: state.fault, lockout: state.mode === MODES.ACTIVE && !outputReady ? 'output readback is not verified' : info.lockout, telemetry: telemetry ? { ...telemetry, telemetryAge: info.age, valid: info.valid } : null, csvLogging: { ...state.csvLogging }, socEntryEligible: finite(telemetry && telemetry.soc) && telemetry.soc > config.socEntryThreshold, socContinuationEligible: finite(telemetry && telemetry.soc) && telemetry.soc > config.socExitThreshold, selectedCurrent: selected && selected.current, selectedSpread: spread, balancerLikelyActive: Boolean(selected && finite(selected.current) && selected.current >= config.balancerMinimumCurrent && finite(spread) && spread > config.balancerSpreadThreshold && command && command.chargeEnabled), aggregateCurrentCommand: state.aggregateCurrentCommand, lastCommand: command, config: { ...config }, outputReady, history: history.slice(), availableEvents: events.slice(-100), lastEvent: state.lastEvent }
  }
  function handle (message = {}) {
    const timestamp = finite(message.timestamp) ? message.timestamp : now()
    if (message.type === 'load') { state = { ...emptyState(), ...(message.state || {}) }; const validated = validateConfig(message.config || {}); config = validated.config; if (!validated.valid) { state.fault = validated.errors.join('; '); state.state = STATES.FAULT_OR_ABORT } return evaluate(timestamp) }
    if (message.type === 'telemetry') { telemetry = { ...message.telemetry }; return evaluate(timestamp) }
    if (message.type === 'tick') return evaluate(timestamp)
    if (message.type === 'set_output_ready') { outputReady = message.value === true; return evaluate(timestamp) }
    if (message.type === 'set_enabled') { state.enabled = message.value === true; if (!state.enabled) { state.fault = null; resetPi(); releaseSelection(); transition(STATES.NORMAL, timestamp, 'controller disabled') } return evaluate(timestamp) }
    if (message.type === 'set_mode') { if (message.value === MODES.ACTIVE && outputReady && validateConfig(config).valid) state.mode = MODES.ACTIVE; else if (message.value === MODES.TEST) state.mode = MODES.TEST; else record(timestamp, 'mode_rejected', 'ACTIVE requires verified output and valid configuration'); return evaluate(timestamp) }
    if (message.type === 'set_auto_manual' && (message.value === 'AUTO' || message.value === 'MANUAL')) { state.autoManualMode = message.value; return evaluate(timestamp) }
    if (message.type === 'set_csv_logging') { const filename = typeof message.filename === 'string' ? message.filename.trim() : null; if (message.value === true && !/^[A-Za-z0-9][A-Za-z0-9._-]{0,80}(?:\.csv)?$/.test(filename || '')) { record(timestamp, 'csv_logging_rejected', 'invalid CSV filename'); return evaluate(timestamp) } const normalized = message.value === true ? (filename.endsWith('.csv') ? filename : `${filename}.csv`) : null; state.csvLogging = { enabled: message.value === true, filename: normalized }; pendingCsvLogging = { ...state.csvLogging, timestamp }; return evaluate(timestamp) }
    if (message.type === 'manual_start') {
      state.autoManualMode = 'MANUAL'
      const manualBattery = message.address
        ? (telemetry && telemetry.batteries || []).find(battery => battery.address === message.address)
        : (telemetry && telemetry.batteries || []).find(battery => eligible(battery, true))
      if (manualBattery) {
        state.selectedAddress = manualBattery.address
        state.selectedReason = 'manual operator selection'
        if (state.state === STATES.NORMAL) transition(STATES.BALANCE_ELIGIBILITY, timestamp, 'manual balancing start')
      } else record(timestamp, 'manual_start_rejected', 'no battery meets mandatory safety eligibility')
      return evaluate(timestamp)
    }
    if (message.type === 'manual_stop') { resetPi(); releaseSelection(); transition(STATES.NORMAL, timestamp, 'manual balancing stop'); return evaluate(timestamp) }
    if (message.type === 'reset_integrator') { resetPi(); record(timestamp, 'integrator_reset', 'operator reset'); return evaluate(timestamp) }
    if (message.type === 'configure') { const validated = validateConfig(message.config); if (validated.valid) config = validated.config; else record(timestamp, 'config_rejected', validated.errors.join('; ')); return evaluate(timestamp) }
    return evaluate(timestamp)
  }
  return { handle, getState: () => ({ ...state }), getConfig: () => ({ ...config }), getStatus: timestamp => status(timestamp || now()), getEvents: () => events.slice() }
}

module.exports = { STATES, MODES, DEFAULT_CONFIG, validateConfig, createBalancerController }
