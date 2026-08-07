'use strict'

const fs = require('fs')
const path = require('path')

const root = path.resolve(__dirname, '..')
const templatePath = path.join(root, 'flow', 'cerbo-balancer-controller.template.json')
const outputPath = path.join(root, 'flow', 'cerbo-balancer-controller.json')

const controller = fs.readFileSync(path.join(root, 'src', 'controller.js'), 'utf8')
const help = fs.readFileSync(path.join(root, 'docs', 'dashboard-help.md'), 'utf8')
const template = fs.readFileSync(templatePath, 'utf8')
const flow = JSON.parse(template
  .replace('__CONTROLLER_SOURCE__', JSON.stringify(controller).slice(1, -1))
  .replace('__DASHBOARD_HELP__', JSON.stringify(help).slice(1, -1)))

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
