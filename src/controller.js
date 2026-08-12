'use strict'

const STATES = Object.freeze({
  NORMAL: 'NORMAL',
  BALANCING: 'BALANCING',
  SAFETY_STOP: 'SAFETY_STOP'
})

const MODES = Object.freeze({ TEST: 'TEST', ACTIVE: 'ACTIVE' })
const CONTROLLER_VERSION = 4

const DEFAULT_CONFIG = Object.freeze({
  balancingVoltageCeiling: 56.5,
  safetyFallbackVoltage: 55.0,
  safetyFallbackCurrent: 10.0,
  balancerSpreadThreshold: 0.030,
  balanceBatteryCurrentTarget: 2.0,
  currentTargetTolerance: 0.25,
  feedForwardAlpha: 0.20,
  shareMinimumSelectedCurrent: 0.25,
  shareMinimumTotalCurrent: 0.5,
  feedForwardFallbackCurrent: 2.0,
  kp: 0.20,
  ki: 0.002,
  maxIntegralTerm: 10.0,
  aggregateCurrentMinimum: 0.0,
  aggregateCurrentMaximum: 100.0,
  currentIncreaseRatePerMin: 10.0,
  solarShortfallTolerance: 2.0,
  solarDetectionSamples: 4,
  permissionRearmDebounceMs: 5000,
  telemetryStaleMs: 20000
})

function finite (value) { return typeof value === 'number' && Number.isFinite(value) }
function clamp (value, minimum, maximum) { return Math.max(minimum, Math.min(maximum, value)) }

function emptyState () {
  return {
    version: CONTROLLER_VERSION,
    state: STATES.NORMAL,
    mode: MODES.TEST,
    automaticBalancingEnabled: true,
    selectedAddress: null,
    selectedReason: null,
    completionLatched: false,
    permissionOffSeen: false,
    permissionOnStartedAt: null,
    integralTerm: 0,
    aggregateCurrentCommand: DEFAULT_CONFIG.feedForwardFallbackCurrent,
    rawSelectedShare: null,
    filteredSelectedShare: null,
    feedForwardCurrent: DEFAULT_CONFIG.feedForwardFallbackCurrent,
    pTerm: 0,
    solarLimited: false,
    solarDeficitSamples: 0,
    solarRecoverySamples: 0,
    lastControlSampleTimestamp: null,
    lastTransitionAt: null,
    lastStopReason: null,
    fault: null,
    lastEvent: null,
    csvLogging: { enabled: false, filename: null }
  }
}

function validateConfig (candidate = {}) {
  const config = { ...DEFAULT_CONFIG, ...candidate }
  // Normal limits are owned by the Cerbo Charge Control UI, not this
  // controller. Drop the former persisted override during migration.
  delete config.normalPackVoltageMax
  // Old persisted values must not shorten the new eight-second telemetry budget.
  config.telemetryStaleMs = Math.max(DEFAULT_CONFIG.telemetryStaleMs, config.telemetryStaleMs)
  const errors = []
  const positive = [
    'balancingVoltageCeiling', 'safetyFallbackVoltage',
    'safetyFallbackCurrent', 'balancerSpreadThreshold', 'balanceBatteryCurrentTarget',
    'currentTargetTolerance', 'feedForwardAlpha', 'shareMinimumSelectedCurrent',
    'shareMinimumTotalCurrent', 'feedForwardFallbackCurrent', 'kp', 'ki',
    'maxIntegralTerm', 'aggregateCurrentMaximum', 'currentIncreaseRatePerMin',
    'solarShortfallTolerance', 'solarDetectionSamples', 'permissionRearmDebounceMs',
    'telemetryStaleMs'
  ]
  for (const field of positive) {
    if (!finite(config[field]) || config[field] <= 0) errors.push(`${field} must be positive`)
  }
  if (!finite(config.aggregateCurrentMinimum) || config.aggregateCurrentMinimum < 0) {
    errors.push('aggregateCurrentMinimum must be non-negative')
  }
  if (!(config.aggregateCurrentMinimum <= config.aggregateCurrentMaximum)) {
    errors.push('aggregate current bounds are invalid')
  }
  if (!(config.feedForwardAlpha > 0 && config.feedForwardAlpha <= 1)) {
    errors.push('feedForwardAlpha must be greater than zero and at most one')
  }
  if (!Number.isInteger(config.solarDetectionSamples)) {
    errors.push('solarDetectionSamples must be an integer')
  }
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
    events.push(event)
    state.lastEvent = event
    return event
  }

  function transition (next, timestamp, reason) {
    if (state.state === next) return
    record(timestamp, 'state_transition', `${state.state} -> ${next}: ${reason}`)
    state.state = next
    state.lastTransitionAt = timestamp
  }

  function resetControl () {
    state.integralTerm = 0
    state.aggregateCurrentCommand = config.feedForwardFallbackCurrent
    state.rawSelectedShare = null
    state.filteredSelectedShare = null
    state.feedForwardCurrent = config.feedForwardFallbackCurrent
    state.pTerm = 0
    state.solarLimited = false
    state.solarDeficitSamples = 0
    state.solarRecoverySamples = 0
    state.lastControlSampleTimestamp = null
  }

  function releaseSelection () {
    state.selectedAddress = null
    state.selectedReason = null
  }

  function migrateState (persisted = {}) {
    const migrated = { ...emptyState(), mode: persisted.mode || MODES.TEST }
    if (typeof persisted.automaticBalancingEnabled === 'boolean') {
      migrated.automaticBalancingEnabled = persisted.automaticBalancingEnabled
    } else if (typeof persisted.enabled === 'boolean') {
      migrated.automaticBalancingEnabled = persisted.enabled
    }
    migrated.csvLogging = persisted.csvLogging || migrated.csvLogging
    migrated.completionLatched = [3, CONTROLLER_VERSION].includes(persisted.version) && persisted.completionLatched === true
    migrated.permissionOffSeen = [3, CONTROLLER_VERSION].includes(persisted.version) && persisted.permissionOffSeen === true
    if ([3, CONTROLLER_VERSION].includes(persisted.version) && Object.values(STATES).includes(persisted.state)) {
      const { enabled, autoManualMode, ...current } = persisted
      Object.assign(migrated, current)
      migrated.version = CONTROLLER_VERSION
      if (typeof persisted.automaticBalancingEnabled !== 'boolean' && typeof enabled === 'boolean') {
        migrated.automaticBalancingEnabled = enabled
      }
      return migrated
    }
    const oldActive = [
      'BALANCE_ELIGIBILITY', 'BALANCE_RESTART', 'BALANCE_CURRENT_CONTROL',
      'BALANCE_CONFIRM'
    ]
    const oldFault = ['FAULT_OR_ABORT', 'PROTECTION_RECOVERY']
    if (oldActive.includes(persisted.state) && Number.isInteger(persisted.selectedAddress)) {
      migrated.state = STATES.BALANCING
      migrated.selectedAddress = persisted.selectedAddress
      migrated.selectedReason = 'migrated selected battery'
    } else if (oldFault.includes(persisted.state)) {
      migrated.state = STATES.SAFETY_STOP
      migrated.fault = persisted.fault || 'migrated controller fault'
    }
    return migrated
  }

  function expectedAddresses () {
    if (!telemetry) return []
    const values = telemetry.expectedBatteries || telemetry.expectedAddresses || []
    return Array.isArray(values) ? values.map(Number).filter(Number.isInteger).sort((a, b) => a - b) : []
  }

  function batteries () {
    return telemetry && Array.isArray(telemetry.batteries) ? telemetry.batteries : []
  }

  function batteryByAddress (address) {
    return batteries().find(battery => battery.address === address) || null
  }

  function localProtection (battery) {
    const active = battery && battery.status44 && battery.status44.status1 && battery.status44.status1.active
    return Array.isArray(active) && active.length > 0
  }

  function chargeMosfetOn (battery) {
    const status = battery && battery.status44 && battery.status44.status2
    return status ? status.chargeMosfet === true : battery && battery.chargeMosfet === true
  }

  function effectiveDischarging (battery) {
    const status = battery && battery.status44 && battery.status44.status3
    return Boolean(status && status.effectiveDischarging === true)
  }

  function battery61Valid (battery) {
    return Boolean(battery && battery.valid === true && battery.system61Valid === true &&
      Number.isInteger(battery.soc) && battery.soc >= 0 && battery.soc <= 100 &&
      finite(battery.vmin) && finite(battery.vmax) && finite(battery.spread) &&
      battery.vmax >= battery.vmin && finite(battery.current))
  }

  function freshTelemetry (timestamp) {
    const age = telemetry && finite(telemetry.timestamp) ? Math.max(0, timestamp - telemetry.timestamp) : Infinity
    const expected = expectedAddresses()
    const complete = expected.length > 0 && expected.every(address => battery61Valid(batteryByAddress(address)))
    const inventoryComplete = expected.length > 0 && !((telemetry && telemetry.inventory && telemetry.inventory.pendingRemoval) || []).length
    const valid = Boolean(telemetry && telemetry.valid === true && age <= config.telemetryStaleMs && complete && inventoryComplete)
    let lockout = null
    if (!telemetry) lockout = 'RS485 telemetry is unavailable'
    else if (age > config.telemetryStaleMs) lockout = 'RS485 telemetry is stale'
    else if (!inventoryComplete) lockout = 'complete fresh telemetry from every expected battery is required'
    else if (!complete) lockout = 'addressed CID2 61 telemetry is incomplete or invalid'
    else if (!telemetry.valid) lockout = telemetry.reason || 'RS485 telemetry is invalid'
    return { age, valid, lockout }
  }

  function limits () { return telemetry && telemetry.limits ? telemetry.limits : {} }
  function chargeControlSettings () {
    return telemetry && telemetry.chargeControlSettings ? telemetry.chargeControlSettings : {}
  }
  function normalChargeVoltage () {
    const settings = chargeControlSettings()
    if (settings.voltageLimitEnabled === true && finite(settings.maxChargeVoltage)) return settings.maxChargeVoltage
    return finite(limits().chargeVoltage) ? limits().chargeVoltage : config.safetyFallbackVoltage
  }
  function normalChargeCurrent () {
    const settings = chargeControlSettings()
    if (settings.currentLimitEnabled === true && finite(settings.maxChargeCurrent)) return Math.max(0, settings.maxChargeCurrent)
    return finite(limits().chargeCurrent) ? Math.max(0, limits().chargeCurrent) : config.safetyFallbackCurrent
  }
  function chargePermitted () { return limits().statusFlags && limits().statusFlags.chargeEnabled === true }
  function ccl () { return finite(telemetry && telemetry.ccl) ? Math.max(0, telemetry.ccl) : null }
  function virtualBmsSelected () { return Boolean(telemetry && telemetry.activeBms && telemetry.activeBms.virtualSelected === true) }

  function eligible (battery) {
    return Boolean(battery61Valid(battery) && battery.spread > config.balancerSpreadThreshold &&
      chargeMosfetOn(battery) && !localProtection(battery))
  }

  function selectCandidate (timestamp, excludedAddress = null, manualAddress = null) {
    const ordered = batteries().slice().sort((a, b) => a.address - b.address)
    const candidate = manualAddress != null
      ? ordered.find(battery => battery.address === manualAddress && battery.address !== excludedAddress && eligible(battery))
      : ordered.find(battery => battery.address !== excludedAddress && eligible(battery))
    if (!candidate) return null
    state.selectedAddress = candidate.address
    state.selectedReason = manualAddress != null ? 'manual operator selection' : 'first eligible battery in address order'
    resetControl()
    transition(STATES.BALANCING, timestamp, `battery ${candidate.address} selected`)
    record(timestamp, 'BATTERY_SELECTED', state.selectedReason, { selectedAddress: candidate.address, spread: candidate.spread })
    return candidate
  }

  function allExpectedAtFullSoc () {
    const expected = expectedAddresses()
    return expected.length > 0 && expected.every(address => {
      const battery = batteryByAddress(address)
      return battery61Valid(battery) && battery.soc === 100
    })
  }

  function updateCompletionRearm (timestamp) {
    if (!state.completionLatched) return
    if (!chargePermitted()) {
      state.permissionOffSeen = true
      state.permissionOnStartedAt = null
      return
    }
    if (!state.permissionOffSeen) return
    state.permissionOnStartedAt = state.permissionOnStartedAt || timestamp
    if (timestamp - state.permissionOnStartedAt >= config.permissionRearmDebounceMs) {
      state.completionLatched = false
      state.permissionOffSeen = false
      state.permissionOnStartedAt = null
      record(timestamp, 'BALANCE_REARMED', 'charge permission remained enabled after an observed disabled state')
    }
  }

  function positiveTotalCurrent () {
    return batteries().reduce((total, battery) => {
      if (!battery61Valid(battery) || !chargeMosfetOn(battery) || localProtection(battery)) return total
      return total + Math.max(0, battery.current)
    }, 0)
  }

  function effectiveControl () { return telemetry && telemetry.effectiveControl ? telemetry.effectiveControl : {} }
  function effectiveCcl () {
    const value = effectiveControl().effectiveChargeCurrent
    return finite(value) ? Math.max(0, value) : ccl()
  }

  function balancingCommand (timestamp, battery) {
    const sampleTimestamp = finite(telemetry && telemetry.timestamp) ? telemetry.timestamp : timestamp
    const selectedCurrent = battery.current
    const totalCurrent = positiveTotalCurrent()
    const error = config.balanceBatteryCurrentTarget - selectedCurrent
    const previousSample = state.lastControlSampleTimestamp
    if (previousSample === sampleTimestamp) return commandFromState(error, 'BALANCING_DUPLICATE_SAMPLE')

    const dt = previousSample == null ? 0 : Math.max(0, (sampleTimestamp - previousSample) / 1000)
    state.lastControlSampleTimestamp = sampleTimestamp
    const heldRequest = state.aggregateCurrentCommand
    const control = effectiveControl()
    const effective = effectiveCcl()
    const thermalFactor = finite(control.thermalFactor) ? control.thermalFactor : 1
    const permission = chargePermitted()
    const bmsLimited = finite(effective) && effective < heldRequest - 0.05
    const thermalLimited = thermalFactor < 0.999
    const canClassifySolar = permission && chargeMosfetOn(battery) && !thermalLimited && !bmsLimited && ccl() !== 0
    const solarDeficit = canClassifySolar && selectedCurrent < config.balanceBatteryCurrentTarget - config.currentTargetTolerance &&
      totalCurrent < heldRequest - config.solarShortfallTolerance

    if (!state.solarLimited) {
      state.solarDeficitSamples = solarDeficit ? state.solarDeficitSamples + 1 : 0
      if (state.solarDeficitSamples >= config.solarDetectionSamples) {
        state.solarLimited = true
        state.solarRecoverySamples = 0
        record(timestamp, 'SOLAR_LIMITED_PAUSE', 'available charging current is below the held aggregate request', {
          selectedAddress: battery.address, selectedCurrent, positiveTotalCurrent: totalCurrent, heldRequest
        })
      }
    }

    if (state.solarLimited) {
      const recovered = Math.abs(error) <= config.currentTargetTolerance ||
        totalCurrent >= heldRequest - config.solarShortfallTolerance
      state.solarRecoverySamples = recovered ? state.solarRecoverySamples + 1 : 0
      if (state.solarRecoverySamples < config.solarDetectionSamples) {
        return commandFromState(error, 'SOLAR_LIMITED_PAUSE', totalCurrent)
      }
      state.solarLimited = false
      state.solarDeficitSamples = 0
      state.solarRecoverySamples = 0
      record(timestamp, 'SOLAR_LIMITED_RESUME', 'available charging current recovered')
    }

    const controllerFrozen = !permission || ccl() === 0 || selectedCurrent <= 0 || bmsLimited || thermalLimited
    if (controllerFrozen) {
      const reason = !permission ? 'BMS_CHARGE_PERMISSION_DISABLED' : ccl() === 0 ? 'BMS_CCL_ZERO' :
        selectedCurrent <= 0 ? 'SELECTED_CURRENT_NOT_POSITIVE' : thermalLimited ? 'THERMAL_LIMITED' : 'BMS_LIMITED'
      return commandFromState(error, reason, totalCurrent)
    }

    if (selectedCurrent >= config.shareMinimumSelectedCurrent && totalCurrent >= config.shareMinimumTotalCurrent) {
      const rawShare = clamp(selectedCurrent / totalCurrent, 0.001, 1)
      state.rawSelectedShare = rawShare
      state.filteredSelectedShare = state.filteredSelectedShare == null
        ? rawShare
        : config.feedForwardAlpha * rawShare + (1 - config.feedForwardAlpha) * state.filteredSelectedShare
    }
    state.feedForwardCurrent = state.filteredSelectedShare == null
      ? config.feedForwardFallbackCurrent
      : config.balanceBatteryCurrentTarget / state.filteredSelectedShare
    state.pTerm = config.kp * error
    let nextIntegral = state.integralTerm
    if (dt > 0) nextIntegral = clamp(nextIntegral + config.ki * error * dt, -config.maxIntegralTerm, config.maxIntegralTerm)
    const desired = clamp(state.feedForwardCurrent + state.pTerm + nextIntegral,
      config.aggregateCurrentMinimum, config.aggregateCurrentMaximum)
    const rise = config.currentIncreaseRatePerMin * dt / 60
    const request = desired > heldRequest ? Math.min(desired, heldRequest + rise) : desired
    const saturated = desired !== state.feedForwardCurrent + state.pTerm + nextIntegral || request < desired
    if (!(saturated && error > 0)) state.integralTerm = nextIntegral
    state.aggregateCurrentCommand = request
    return commandFromState(error, 'BALANCING_CURRENT_CONTROL', totalCurrent, saturated)
  }

  function commandFromState (error, reason, totalCurrent = positiveTotalCurrent(), saturated = false) {
    const settings = chargeControlSettings()
    const requestedVoltage = settings.voltageLimitEnabled === true && finite(settings.maxChargeVoltage)
      ? Math.min(config.balancingVoltageCeiling, settings.maxChargeVoltage)
      : config.balancingVoltageCeiling
    return {
      requestedVoltage,
      requestedCurrent: state.aggregateCurrentCommand,
      chargeEnabled: true,
      reason,
      pTerm: state.pTerm,
      iTerm: state.integralTerm,
      currentError: error,
      feedForwardCurrent: state.feedForwardCurrent,
      rawSelectedShare: state.rawSelectedShare,
      filteredSelectedShare: state.filteredSelectedShare,
      positiveTotalCurrent: totalCurrent,
      outputSaturated: saturated,
      solarLimited: state.solarLimited
    }
  }

  function normalCommand (reason) {
    return { requestedVoltage: normalChargeVoltage(), requestedCurrent: normalChargeCurrent(), chargeEnabled: true, reason }
  }

  function safetyCommand (reason) {
    return { requestedVoltage: config.safetyFallbackVoltage, requestedCurrent: config.safetyFallbackCurrent, chargeEnabled: true, reason }
  }

  function enterSafety (timestamp, reason) {
    state.fault = reason
    resetControl()
    releaseSelection()
    transition(STATES.SAFETY_STOP, timestamp, reason)
  }

  function evaluate (timestamp) {
    const actions = []
    const health = freshTelemetry(timestamp)
    let command = normalCommand('NORMAL_POLICY')

    if (!state.automaticBalancingEnabled) {
      state.fault = null
      resetControl()
      releaseSelection()
      transition(STATES.NORMAL, timestamp, 'automatic balancing disabled')
      command = normalCommand('AUTOMATIC_BALANCING_OFF')
    } else if (state.mode === MODES.ACTIVE && !virtualBmsSelected()) {
      state.fault = null
      resetControl()
      releaseSelection()
      transition(STATES.NORMAL, timestamp, 'virtual BMS is not selected')
      command = normalCommand('VIRTUAL_BMS_NOT_SELECTED')
    } else if (!health.valid) {
      enterSafety(timestamp, health.lockout)
      command = safetyCommand('SAFETY_STOP_TELEMETRY')
    } else if (state.mode === MODES.ACTIVE && !outputReady) {
      enterSafety(timestamp, 'output readback is not verified')
      command = safetyCommand('SAFETY_STOP_OUTPUT')
    } else {
      if (state.state === STATES.SAFETY_STOP) {
        state.fault = null
        transition(STATES.NORMAL, timestamp, 'telemetry and output control recovered')
      }
      updateCompletionRearm(timestamp)
      let selected = batteryByAddress(state.selectedAddress)

      if (state.state === STATES.BALANCING && allExpectedAtFullSoc()) {
        state.completionLatched = true
        state.permissionOffSeen = false
        state.permissionOnStartedAt = null
        state.lastStopReason = 'FULL_SOC_COMPLETE'
        record(timestamp, 'FULL_SOC_COMPLETE', 'all expected batteries report integer SOC 100')
        resetControl()
        releaseSelection()
        transition(STATES.NORMAL, timestamp, 'all expected batteries reached full SOC')
        command = normalCommand('FULL_SOC_COMPLETE')
      } else if (state.state === STATES.BALANCING && (!selected || !chargeMosfetOn(selected) || localProtection(selected))) {
        const releasedAddress = state.selectedAddress
        record(timestamp, 'BATTERY_CHARGE_PATH_INTERRUPTED', 'selected battery is disconnected or protected', { selectedAddress: releasedAddress })
        resetControl()
        releaseSelection()
        selected = selectCandidate(timestamp, releasedAddress)
        if (!selected) transition(STATES.NORMAL, timestamp, 'no other eligible battery')
        command = selected ? balancingCommand(timestamp, selected) : normalCommand('NO_ELIGIBLE_BATTERY')
      } else if (state.state === STATES.BALANCING && selected && effectiveDischarging(selected) &&
        selected.soc < 100 && selected.spread < config.balancerSpreadThreshold) {
        state.lastStopReason = 'BALANCE_DISCHARGE_COMPLETE'
        record(timestamp, 'BALANCE_DISCHARGE_COMPLETE', 'effective discharge with SOC below 100 and spread below threshold', { selectedAddress: selected.address })
        resetControl()
        releaseSelection()
        transition(STATES.NORMAL, timestamp, 'selected battery discharge completion')
        command = normalCommand('BALANCE_DISCHARGE_COMPLETE')
      } else if (state.state === STATES.BALANCING && selected) {
        command = balancingCommand(timestamp, selected)
      } else if (state.state === STATES.NORMAL && !state.completionLatched) {
        selected = selectCandidate(timestamp)
        command = selected ? balancingCommand(timestamp, selected) : normalCommand('NO_ELIGIBLE_BATTERY')
      }
    }

    const controllerCommand = { version: timestamp, timestamp, mode: state.mode, ...command }
    const selected = batteryByAddress(state.selectedAddress)
    history.push({
      timestamp,
      state: state.state,
      selectedAddress: state.selectedAddress,
      selectedCurrent: selected && finite(selected.current) ? selected.current : null,
      selectedSoc: selected && Number.isInteger(selected.soc) ? selected.soc : null,
      selectedVmax: selected && finite(selected.vmax) ? selected.vmax : null,
      selectedVmin: selected && finite(selected.vmin) ? selected.vmin : null,
      selectedSpread: selected && finite(selected.spread) ? selected.spread : null,
      positiveTotalCurrent: positiveTotalCurrent(),
      aggregateCurrentCommand: state.aggregateCurrentCommand,
      effectiveCcl: effectiveCcl(),
      solarLimited: state.solarLimited
    })
    if (history.length > 360) history.splice(0, history.length - 360)

    const persistedState = { ...state }
    const persistence = { state: persistedState, config: { ...config } }
    const persistenceKey = JSON.stringify(persistence)
    actions.push({ type: 'controller_command', value: controllerCommand })
    if (pendingCsvLogging) {
      actions.push({ type: 'csv_logging', value: pendingCsvLogging })
      pendingCsvLogging = null
    }
    if (persistenceKey !== lastPersistenceKey) {
      lastPersistenceKey = persistenceKey
      actions.push({ type: 'persist', ...persistence })
    }
    actions.push({ type: 'status', status: status(timestamp, health, controllerCommand) })
    return actions
  }

  function status (timestamp, health = freshTelemetry(timestamp), command = null) {
    const selected = batteryByAddress(state.selectedAddress)
    const excludedBatteries = batteries().filter(battery => !eligible(battery)).map(battery => ({
      address: battery.address,
      mosfetOn: chargeMosfetOn(battery),
      protected: localProtection(battery),
      system61Valid: battery.system61Valid === true,
      spread: finite(battery.spread) ? battery.spread : null
    }))
    const selectionLockout = state.mode === MODES.ACTIVE && !virtualBmsSelected()
      ? 'RS485 virtual BMS is not selected in Cerbo'
      : null
    return {
      state: state.state,
      mode: state.mode,
      automaticBalancingEnabled: state.automaticBalancingEnabled,
      selectedAddress: state.selectedAddress,
      selectedReason: state.selectedReason,
      lastStopReason: state.lastStopReason,
      fault: state.fault,
      lockout: selectionLockout || (state.mode === MODES.ACTIVE && !outputReady ? 'output readback is not verified' : health.lockout),
      telemetry: telemetry ? { ...telemetry, telemetryAge: health.age, valid: health.valid } : null,
      activeBms: telemetry && telemetry.activeBms ? { ...telemetry.activeBms } : null,
      virtualBmsSelected: virtualBmsSelected(),
      virtualBms: telemetry && telemetry.effectiveControl ? { ...telemetry.effectiveControl } : null,
      chargeControlSettings: { ...chargeControlSettings() },
      csvLogging: { ...state.csvLogging },
      selectedCurrent: selected && selected.current,
      selectedSoc: selected && selected.soc,
      selectedVmin: selected && selected.vmin,
      selectedVmax: selected && selected.vmax,
      selectedSpread: selected && selected.spread,
      selectedEffectiveDischarging: selected ? effectiveDischarging(selected) : false,
      positiveTotalCurrent: positiveTotalCurrent(),
      rawSelectedShare: state.rawSelectedShare,
      filteredSelectedShare: state.filteredSelectedShare,
      feedForwardCurrent: state.feedForwardCurrent,
      pTerm: state.pTerm,
      iTerm: state.integralTerm,
      aggregateCurrentCommand: state.aggregateCurrentCommand,
      solarLimited: state.solarLimited,
      solarDeficitSamples: state.solarDeficitSamples,
      solarRecoverySamples: state.solarRecoverySamples,
      completionLatched: state.completionLatched,
      permissionOffSeen: state.permissionOffSeen,
      permissionOnStartedAt: state.permissionOnStartedAt,
      excludedBatteries,
      lastCommand: command,
      config: { ...config },
      outputReady,
      history: history.slice(),
      availableEvents: events.slice(-100),
      lastEvent: state.lastEvent
    }
  }

  function handle (message = {}) {
    const timestamp = finite(message.timestamp) ? message.timestamp : now()
    if (message.type === 'load') {
      const currentVersion = message.state && [3, CONTROLLER_VERSION].includes(message.state.version)
      state = migrateState(message.state || {})
      const validated = validateConfig(currentVersion ? (message.config || {}) : {})
      config = validated.config
      resetControl()
      if (!validated.valid) {
        state.fault = validated.errors.join('; ')
        state.state = STATES.SAFETY_STOP
      }
      return evaluate(timestamp)
    }
    if (message.type === 'telemetry') {
      telemetry = { ...message.telemetry }
      return evaluate(timestamp)
    }
    if (message.type === 'tick') return evaluate(timestamp)
    if (message.type === 'set_output_ready') {
      outputReady = message.value === true
      return evaluate(timestamp)
    }
    if (message.type === 'set_automatic_balancing') {
      state.automaticBalancingEnabled = message.value === true
      if (!state.automaticBalancingEnabled) {
        state.fault = null
        resetControl()
        releaseSelection()
        transition(STATES.NORMAL, timestamp, 'automatic balancing disabled')
      }
      return evaluate(timestamp)
    }
    if (message.type === 'set_mode') {
      if (message.value === MODES.ACTIVE && outputReady && virtualBmsSelected() && validateConfig(config).valid) state.mode = MODES.ACTIVE
      else if (message.value === MODES.TEST) state.mode = MODES.TEST
      else record(timestamp, 'mode_rejected', 'ACTIVE requires selected virtual BMS, verified output, and valid configuration')
      return evaluate(timestamp)
    }
    if (message.type === 'set_csv_logging') {
      const filename = typeof message.filename === 'string' ? message.filename.trim() : null
      if (message.value === true && !/^[A-Za-z0-9][A-Za-z0-9._-]{0,80}(?:\.csv)?$/.test(filename || '')) {
        record(timestamp, 'csv_logging_rejected', 'invalid CSV filename')
        return evaluate(timestamp)
      }
      const normalized = message.value === true ? (filename.endsWith('.csv') ? filename : `${filename}.csv`) : null
      state.csvLogging = { enabled: message.value === true, filename: normalized }
      pendingCsvLogging = { ...state.csvLogging, timestamp }
      return evaluate(timestamp)
    }
    if (message.type === 'reset_integrator') {
      resetControl()
      record(timestamp, 'integrator_reset', 'operator reset')
      return evaluate(timestamp)
    }
    if (message.type === 'restore_defaults') {
      config = { ...DEFAULT_CONFIG }
      state.automaticBalancingEnabled = true
      state.completionLatched = false
      state.permissionOffSeen = false
      state.permissionOnStartedAt = null
      state.lastStopReason = null
      state.fault = null
      resetControl()
      releaseSelection()
      transition(STATES.NORMAL, timestamp, 'controller defaults restored')
      record(timestamp, 'defaults_restored', 'automatic balancing enabled and controller defaults restored')
      return evaluate(timestamp)
    }
    if (message.type === 'configure') {
      const validated = validateConfig(message.config)
      if (validated.valid) config = validated.config
      else record(timestamp, 'config_rejected', validated.errors.join('; '))
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

module.exports = { STATES, MODES, CONTROLLER_VERSION, DEFAULT_CONFIG, validateConfig, createBalancerController }
