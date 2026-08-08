'use strict'

const fs = require('fs')
const path = require('path')

const root = path.resolve(__dirname, '..')
const templatePath = path.join(root, 'flow', 'cerbo-balancer-controller.template.json')
const outputPath = path.join(root, 'flow', 'cerbo-balancer-controller.json')

const controller = fs.readFileSync(path.join(root, 'src', 'controller.js'), 'utf8')
const maintenanceFormatter = fs.readFileSync(path.join(root, 'src', 'maintenance_formatter.js'), 'utf8')
const maintenanceView = fs.readFileSync(path.join(root, 'src', 'maintenance_view.html'), 'utf8')
const help = fs.readFileSync(path.join(root, 'docs', 'dashboard-help.md'), 'utf8')
const template = fs.readFileSync(templatePath, 'utf8')
const flow = JSON.parse(template
  .replaceAll('effective_cells', 'effectiveCells')
  .replaceAll('directly_reported_cell_sum', 'directlyReportedCellSum')
  .replaceAll('reconstructed_cell_sum', 'reconstructedCellSum')
  .replaceAll('reconstructed_voltage_delta_mv', 'reconstructedVoltageDeltaMv')
  .replaceAll('validation_errors', 'validationErrors')
  .replace("intent ? { topic: 'shadow_command', payload: JSON.stringify(intent) }", "intent ? { topic: 'controller_command', payload: JSON.stringify({ version: Date.now(), timestamp: Date.now(), mode: controller.getState().mode, requestedVoltage: intent.value, requestedCurrent: intent.command && intent.command.chargeCurrentCommand }) }" )
  .replace('__DASHBOARD_HELP__', JSON.stringify(help).slice(1, -1)))

const controllerNode = flow.find(node => node.id === 'bal-controller')
if (!controllerNode) throw new Error('bal-controller node is missing from the flow template')
controllerNode.func = controllerNode.func.replace(
  'const source = "__CONTROLLER_SOURCE__";',
  `const source = Buffer.from(${JSON.stringify(Buffer.from(controller, 'utf8').toString('base64'))}, 'base64').toString('utf8');`
)
const rs485ServiceNode = flow.find(node => node.id === 'bal-rs485-service')
if (!rs485ServiceNode) throw new Error('bal-rs485-service node is missing from the flow template')
rs485ServiceNode.command += ' --no-serial-starter-stop'
const maintenanceFormatterNode = flow.find(node => node.id === 'bal-maint-format')
if (!maintenanceFormatterNode) throw new Error('bal-maint-format node is missing from the flow template')
maintenanceFormatterNode.func = maintenanceFormatter
const maintenanceViewNode = flow.find(node => node.id === 'bal-maintenance')
if (!maintenanceViewNode) throw new Error('bal-maintenance node is missing from the flow template')
maintenanceViewNode.format = maintenanceView

const rendered = `${JSON.stringify(flow, null, 2)}\n`
if (process.argv.includes('--check')) {
  const existing = fs.readFileSync(outputPath, 'utf8')
  if (existing !== rendered) {
    console.error('Generated flow is out of date. Run npm run build-flow.')
    process.exitCode = 1
  }
} else {
  fs.writeFileSync(outputPath, rendered)
}
