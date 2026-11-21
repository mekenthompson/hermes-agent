import { afterEach, describe, expect, it } from 'vitest'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { chmodSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { spawnSync } from 'node:child_process'

const repoRoot = resolve(import.meta.dirname, '../../..')
const runner = join(repoRoot, '.github/scripts/run-workspace-checks.mjs')
const fixtures = []

afterEach(async () => {
  await Promise.all(fixtures.splice(0).map(path => rm(path, { recursive: true, force: true })))
})

async function fixture() {
  const dir = await mkdtemp(join(tmpdir(), 'workspace-checks-'))
  fixtures.push(dir)
  const bin = join(dir, 'bin')
  await mkdir(bin)
  const packages = ['pass-a', 'fail', 'pass-b']
  await writeFile(join(dir, 'package.json'), JSON.stringify({ name: 'scheduler-fixture', private: true }))
  await writeFile(join(dir, 'pnpm-workspace.yaml'), `packages:\n${packages.map(name => `  - ${name}\n`).join('')}`)
  await Promise.all(
    packages.map(async name => {
      await mkdir(join(dir, name))
      await writeFile(join(dir, name, 'package.json'), JSON.stringify({ name, scripts: { check: 'true' } }))
    })
  )
  const fakePnpm = `#!/usr/bin/env node
const assert = require('node:assert/strict')
const { appendFileSync } = require('node:fs')
const args = process.argv.slice(2)
assert.equal(process.cwd(), ${JSON.stringify(dir)})
if (args[0] === 'ls') {
  assert.deepEqual(args, ['ls', '--recursive', '--depth', '-1', '--json'])
  process.stdout.write(JSON.stringify(${JSON.stringify([dir, ...packages.map(name => join(dir, name))].map(path => ({ path })))}))
  process.exit(0)
}
assert.equal(args[0], '--dir')
assert.deepEqual(args.slice(2), ['run', 'check'])
const pkg = args[1]
assert.ok(${JSON.stringify(packages)}.includes(pkg))
appendFileSync(process.env.EVENTS, JSON.stringify({ event: 'start', pkg, at: Date.now() }) + '\\n')
setTimeout(() => {
  appendFileSync(process.env.EVENTS, JSON.stringify({ event: 'end', pkg, at: Date.now() }) + '\\n')
  process.exit(pkg === 'fail' ? 1 : 0)
}, 100)
`
  await writeFile(join(bin, 'pnpm'), fakePnpm)
  chmodSync(join(bin, 'pnpm'), 0o755)
  await writeFile(join(dir, 'events'), '')
  return { dir, events: join(dir, 'events') }
}

describe('run-workspace-checks scheduler', () => {
  it('limits active checks to its budget and reports every check after a failure', async () => {
    const { dir, events } = await fixture()
    const result = spawnSync(process.execPath, [runner], {
      cwd: dir,
      encoding: 'utf8',
      env: {
        ...process.env,
        EVENTS: events,
        PATH: `${join(dir, 'bin')}:${process.env.PATH}`,
        WORKSPACE_CHECK_CONCURRENCY: '2'
      }
    })

    expect(result.status).toBe(1)
    expect(result.stdout).toContain('running 3 checks, up to 2 at a time:')
    expect(result.stderr).toContain('1 of 3 checks failed')

    const entries = (await readFile(events, 'utf8')).trim().split('\n').map(JSON.parse)
    expect(entries.filter(entry => entry.event === 'start')).toHaveLength(3)
    expect(entries.filter(entry => entry.event === 'end')).toHaveLength(3)
    for (const event of ['start', 'end']) {
      expect(
        entries
          .filter(entry => entry.event === event)
          .map(entry => entry.pkg)
          .sort()
      ).toEqual(['fail', 'pass-a', 'pass-b'])
    }

    let active = 0
    let maxActive = 0
    for (const entry of entries) {
      active += entry.event === 'start' ? 1 : -1
      maxActive = Math.max(maxActive, active)
    }
    expect(maxActive).toBeLessThanOrEqual(2)
  })
})
