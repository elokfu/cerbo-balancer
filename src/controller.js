'use strict'

const STATES = Object.freeze({
  NORMAL: 'NORMAL',
  EQUALIZE_ENTRY: 'EQUALIZE_ENTRY',
  EQUALIZING: 'EQUALIZING',
  BALANCE_CONFIRM: 'BALANCE_CONFIRM',
  FAULT_OR_ABORT: 'FAULT_OR_ABORT'
})

const MODES = Object.freeze({ TEST: 'TEST', ACTIVE: 'ACTIVE' })

const DEFAULT_CONFIG = Object.freeze({
  normalPackVoltageMax: 55.2,
  equalizationPackVoltageMin: 53.0,
  equalizationPackVoltageMax: 56.8,
  vmaxTarget: 3.550,
  vminCompletionThreshold: 3.450,
  equalizationStartSpread: 0.030,
  balanceCompleteSpread: 0.010,
  equalizationEvaluationMinVmax: 3.425,
  equalizationStartPersistenceMs: 60 * 1000,
  balanceConfirmationMs: 15 * 60 * 1000,
  warningCellVoltage: 3.580,
  softwareEmergencyCellVoltage: 3.590,
  dynessMaximumCellVoltage: 3.600,
  emergencyPackVoltageCommand: 53.0,
  telemetryStaleMs: 5000,
  maxVoltageIncreaseRatePerMin: 0.2,
  maxVoltageDecreaseRatePerMin: 1.0,
  kp: null,
  ki: null,
  kff: 16,
  maxIntegralTerm: 2.0
})

function finite (value) {
  return typeof value === 'number' && Number.isFinite(value)
}

function emptyState () {
  return {
    version: 1,
    state: STATES.NORMAL,
    mode: MODES.TEST,
    enabled: false,
    autoManualMode: 'AUTO',
    entryStartedAt: null,
    confirmationStartedAt: null,
    lastTransitionAt: null,
    lastCommand: null,
    integralTerm: 0,
    lastEvaluationAt: null,
    fault: null,
    lastEvent: null
  }
}

function validateConfig (candidate = {}) {
  const config = { ...DEFAULT_CONFIG, ...candidate }
  const errors = []
  const positive = [
    'normalPackVoltageMax', 'equalizationPackVoltageMin', 'equalizationPackVoltageMax',
    'vmaxTarget', 'vminCompletionThreshold', 'equalizationStartSpread',
    'balanceCompleteSpread', 'equalizationEvaluationMinVmax',
    'equalizationStartPersistenceMs', 'balanceConfirmationMs', 'warningCellVoltage',
    'softwareEmergencyCellVoltage', 'dynessMaximumCellVoltage', 'telemetryStaleMs',
    'maxVoltageIncreaseRatePerMin', 'maxVoltageDecreaseRatePerMin', 'kff', 'maxIntegralTerm'
  ]
  for (const field of positive) if (!finite(config[field]) || config[field] <= 0) errors.push(`${field} must be positive`)
  if (!(config.balanceCompleteSpread < config.equalizationStartSpread)) errors.push('completion spread must be below start spread')
  if (!(config.vminCompletionThreshold < config.vmaxTarget)) errors.push('completion Vmin must be below Vmax target')
  if (!(config.vmaxTarget < config.warningCellVoltage)) errors.push('Vmax target must be below warning voltage')
  if (!(config.warningCellVoltage < config.softwareEmergencyCellVoltage)) errors.push('warning voltage must be below emergency voltage')
  if (!(config.softwareEmergencyCellVoltage < config.dynessMaximumCellVoltage)) errors.push('emergency voltage must be below Dyness maximum')
  if (!(config.equalizationPackVoltageMin < config.equalizationPackVoltageMax)) errors.push('equalization voltage bounds are invalid')
  if (config.kp !== null && (!finite(config.kp) || config.kp < 0)) errors.push('kp must be null or non-negative')
  if (config.ki !== null && (!finite(config.ki) || config.ki < 0)) errors.push('ki must be null or non-negative')
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

  function telemetryStatus (timestamp) {
    const age = telemetry && finite(telemetry.telemetryAge) ? telemetry.telemetryAge : null
    const fresh = age !== null && age <= config.telemetryStaleMs
    const valid = Boolean(telemetry && telemetry.valid && telemetry.cellTelemetryValid && fresh)
    return {
      valid,
      fresh,
      age,
      cellTelemetryValid: Boolean(telemetry && telemetry.cellTelemetryValid),
      lockout: valid ? null : 'valid Vmax/Vmin cell telemetry is required'
    }
  }

  function recordEvent (timestamp, type, detail) {
    const event = { timestamp, date: localDate(timestamp), type, detail }
    events.push(event)
    state.lastEvent = event
    return event
  }

  function transition (next, timestamp, reason) {
    if (state.state === next) return null
    const event = recordEvent(timestamp, 'state_transition', `${state.state} -> ${next}: ${reason}`)
    state.state = next
    state.lastTransitionAt = timestamp
    return event
  }

  function commandCeiling (timestamp) {
    const t = telemetry
    const status = telemetryStatus(timestamp)
    const baseline = state.state === STATES.NORMAL || state.state === STATES.EQUALIZE_ENTRY
      ? config.normalPackVoltageMax
      : (state.lastCommand || config.normalPackVoltageMax)
    const cellSpread = t && finite(t.vmax) && finite(t.vmin) ? t.vmax - t.vmin : null
    const highCell = t && finite(t.vmax) && t.vmax >= config.vmaxTarget
    const warning = t && finite(t.vmax) && t.vmax >= config.warningCellVoltage
    const emergency = t && finite(t.vmax) && t.vmax >= config.softwareEmergencyCellVoltage
    const hard = t && finite(t.vmax) && t.vmax >= config.dynessMaximumCellVoltage
    let pTerm = 0
    let iTerm = state.integralTerm
    let feedForwardTerm = 0
    let raw = baseline
    let highCellOverride = null
    let antiWindup = false

    if (status.valid && state.state === STATES.EQUALIZING && finite(config.kp) && finite(config.ki)) {
      const dt = state.lastEvaluationAt === null ? 0 : Math.max(0, (timestamp - state.lastEvaluationAt) / 1000)
      const error = config.vmaxTarget - t.vmax
      pTerm = config.kp * error
      if (!warning && !emergency && dt > 0) iTerm = Math.max(-config.maxIntegralTerm, Math.min(config.maxIntegralTerm, iTerm + config.ki * error * dt))
      else antiWindup = true
      if (highCell && t.vmax > config.vmaxTarget) feedForwardTerm = -config.kff * (t.vmax - config.vmaxTarget)
      raw = baseline + pTerm + iTerm + feedForwardTerm
    }

    if (emergency || hard) {
      raw = Math.min(raw, config.emergencyPackVoltageCommand)
      highCellOverride = hard ? 'HARD_BMS_LIMIT' : 'SOFTWARE_EMERGENCY'
      antiWindup = true
    } else if (warning) {
      raw = Math.min(raw, baseline)
      highCellOverride = 'WARNING'
      antiWindup = true
    } else if (highCell) {
      raw = Math.min(raw, baseline + feedForwardTerm)
      highCellOverride = 'TARGET'
    }

    const bmsCvl = t && finite(t.cvl) ? t.cvl : null
    let upper = state.state === STATES.NORMAL ? config.normalPackVoltageMax : config.equalizationPackVoltageMax
    if (bmsCvl !== null) upper = Math.min(upper, bmsCvl)
    let limited = Math.max(config.equalizationPackVoltageMin, Math.min(upper, raw))
    if (state.lastCommand !== null && state.lastEvaluationAt !== null) {
      const minutes = Math.max(0, (timestamp - state.lastEvaluationAt) / 60000)
      const rise = config.maxVoltageIncreaseRatePerMin * minutes
      const fall = config.maxVoltageDecreaseRatePerMin * minutes
      limited = Math.min(state.lastCommand + rise, Math.max(state.lastCommand - fall, limited))
      if (limited !== raw) antiWindup = true
    }
    if (state.lastCommand !== null && limited < state.lastCommand && (warning || emergency || hard)) limited = Math.min(limited, state.lastCommand)
    if (antiWindup) iTerm = state.integralTerm
    state.integralTerm = iTerm
    return {
      packVoltageCommand: limited,
      rawControllerOutput: raw,
      pTerm,
      iTerm,
      feedForwardTerm,
      cellSpread,
      highCellOverride,
      outputSaturated: limited !== raw,
      antiWindup,
      bmsCvl
    }
  }

  function evaluate (timestamp) {
    const actions = []
    const t = telemetry
    const telemetryInfo = telemetryStatus(timestamp)
    if (state.enabled && state.mode === MODES.ACTIVE && !telemetryInfo.valid) {
      state.fault = telemetryInfo.lockout
      transition(STATES.FAULT_OR_ABORT, timestamp, telemetryInfo.lockout)
    }
    if (state.state === STATES.FAULT_OR_ABORT) {
      state.lastCommand = config.normalPackVoltageMax
      actions.push({ type: 'voltage_intent', value: config.normalPackVoltageMax, reason: state.fault || 'fault-safe conservative ceiling' })
    } else if (telemetryInfo.valid && t) {
      const spread = t.vmax - t.vmin
      if (state.state === STATES.NORMAL) {
        if (state.autoManualMode === 'AUTO' && spread > config.equalizationStartSpread && t.vmax >= config.equalizationEvaluationMinVmax) {
          if (state.entryStartedAt === null) state.entryStartedAt = timestamp
          if (timestamp - state.entryStartedAt >= config.equalizationStartPersistenceMs) transition(STATES.EQUALIZING, timestamp, `spread ${(spread * 1000).toFixed(1)} mV persisted`)
          else transition(STATES.EQUALIZE_ENTRY, timestamp, 'upper-region spread detected')
        } else state.entryStartedAt = null
      } else if (state.state === STATES.EQUALIZE_ENTRY) {
        if (!(spread > config.equalizationStartSpread && t.vmax >= config.equalizationEvaluationMinVmax)) {
          state.entryStartedAt = null
          transition(STATES.NORMAL, timestamp, 'entry condition cleared')
        } else if (timestamp - state.entryStartedAt >= config.equalizationStartPersistenceMs) transition(STATES.EQUALIZING, timestamp, 'entry persistence complete')
      } else if (state.state === STATES.EQUALIZING && t.vmin > config.vminCompletionThreshold && spread < config.balanceCompleteSpread) {
        if (state.confirmationStartedAt === null) state.confirmationStartedAt = timestamp
        if (timestamp - state.confirmationStartedAt >= config.balanceConfirmationMs) {
          transition(STATES.NORMAL, timestamp, 'balance confirmation complete')
          state.confirmationStartedAt = null
        } else transition(STATES.BALANCE_CONFIRM, timestamp, 'balance criteria held')
      } else if (state.state === STATES.BALANCE_CONFIRM) {
        if (!(t.vmin > config.vminCompletionThreshold && spread < config.balanceCompleteSpread)) {
          state.confirmationStartedAt = null
          transition(STATES.EQUALIZING, timestamp, 'balance criteria lost')
        } else if (timestamp - state.confirmationStartedAt >= config.balanceConfirmationMs) {
          state.confirmationStartedAt = null
          transition(STATES.NORMAL, timestamp, 'balance confirmation complete')
        }
      }
      const command = commandCeiling(timestamp)
      state.lastCommand = command.packVoltageCommand
      actions.push({ type: 'voltage_intent', value: command.packVoltageCommand, reason: state.mode === MODES.TEST ? 'test shadow command' : 'active controller command', command })
    }
    state.lastEvaluationAt = timestamp
    actions.push({ type: 'persist', state: { ...state }, config: { ...config } })
    actions.push({ type: 'status', status: status(timestamp) })
    return actions
  }

  function status (timestamp) {
    const info = telemetryStatus(timestamp)
    const command = state.lastCommand
    return {
      state: state.state,
      mode: state.mode,
      enabled: state.enabled,
      autoManualMode: state.autoManualMode,
      fault: state.fault,
      lockout: state.mode === MODES.ACTIVE && !outputReady ? 'output readback is not verified' : info.lockout,
      telemetry: telemetry ? { ...telemetry, telemetryAge: info.age, valid: info.valid } : null,
      packVoltageCommand: command,
      controllerError: telemetry && finite(telemetry.vmax) ? DEFAULT_CONFIG.vmaxTarget - telemetry.vmax : null,
      lastEvent: state.lastEvent,
      config: { ...config },
      outputReady,
      availableEvents: events.slice(-100)
    }
  }

  function handle (message = {}) {
    const timestamp = finite(message.timestamp) ? message.timestamp : now()
    const type = message.type
    if (type === 'load') {
      state = { ...emptyState(), ...(message.state || {}) }
      const validated = validateConfig(message.config || {})
      config = validated.config
      if (!validated.valid) {
        state.fault = validated.errors.join('; ')
        state.state = STATES.FAULT_OR_ABORT
      }
      return evaluate(timestamp)
    }
    if (type === 'telemetry') {
      telemetry = { ...message.telemetry, telemetryAge: finite(message.telemetry && message.telemetry.timestamp) ? timestamp - message.telemetry.timestamp : null }
      return evaluate(timestamp)
    }
    if (type === 'tick') return evaluate(timestamp)
    if (type === 'set_output_ready') {
      outputReady = message.value === true
      return evaluate(timestamp)
    }
    if (type === 'set_enabled') {
      state.enabled = message.value === true
      if (!state.enabled) { state.fault = null; transition(STATES.NORMAL, timestamp, 'controller disabled') }
      return evaluate(timestamp)
    }
    if (type === 'set_mode') {
      if (message.value === MODES.ACTIVE) {
        const info = telemetryStatus(timestamp)
        const validated = validateConfig(config)
        if (!info.valid || !outputReady || !validated.valid || !finite(config.kp) || !finite(config.ki)) {
          recordEvent(timestamp, 'mode_rejected', 'ACTIVE requires valid cell telemetry, verified output, and configured PI gains')
        } else state.mode = MODES.ACTIVE
      } else if (message.value === MODES.TEST) state.mode = MODES.TEST
      return evaluate(timestamp)
    }
    if (type === 'set_auto_manual') {
      if (message.value === 'AUTO' || message.value === 'MANUAL') state.autoManualMode = message.value
      return evaluate(timestamp)
    }
    if (type === 'manual_start') {
      state.autoManualMode = 'MANUAL'
      if (state.state !== STATES.FAULT_OR_ABORT) transition(STATES.EQUALIZING, timestamp, 'manual equalization start')
      return evaluate(timestamp)
    }
    if (type === 'manual_stop') {
      transition(STATES.NORMAL, timestamp, 'manual equalization stop')
      state.confirmationStartedAt = null
      return evaluate(timestamp)
    }
    if (type === 'reset_integrator') {
      state.integralTerm = 0
      recordEvent(timestamp, 'integrator_reset', 'operator reset')
      return evaluate(timestamp)
    }
    if (type === 'configure') {
      const validated = validateConfig(message.config)
      if (validated.valid) config = validated.config
      else recordEvent(timestamp, 'config_rejected', validated.errors.join('; '))
      return evaluate(timestamp)
    }
    return evaluate(timestamp)
  }

  return {
    handle,
    getState: () => ({ ...state }),
    getConfig: () => ({ ...config }),
    getStatus: timestamp => status(timestamp || now()),
    getEvents: () => events.slice()
  }
}

module.exports = { STATES, MODES, DEFAULT_CONFIG, validateConfig, createBalancerController }
