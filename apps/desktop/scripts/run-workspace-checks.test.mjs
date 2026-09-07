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
  const fakeNpm = `#!/usr/bin/env node
const { appendFileSync } = require('node:fs')
const args = process.argv.slice(2)
if (args[0] === 'query') {
  process.stdout.write(JSON.stringify([
    { location: '/pass-a', scripts: { check: 'true' } },
    { location: '/fail', scripts: { check: 'true' } },
    { location: '/pass-b', scripts: { check: 'true' } },
  ]))
  process.exit(0)
}
const prefix = args[args.indexOf('--prefix') + 1]
appendFileSync(process.env.EVENTS, JSON.stringify({ event: 'start', prefix, at: Date.now() }) + '\\n')
setTimeout(() => {
  appendFileSync(process.env.EVENTS, JSON.stringify({ event: 'end', prefix, at: Date.now() }) + '\\n')
  process.exit(prefix === '/fail' ? 1 : 0)
}, 100)
`
  await writeFile(join(bin, 'npm'), fakeNpm)
  chmodSync(join(bin, 'npm'), 0o755)
  await writeFile(join(dir, 'events'), '')
  return { dir, events: join(dir, 'events') }
}

describe('run-workspace-checks scheduler', () => {
  it('limits active checks to its budget and reports every check after a failure', async () => {
    const { dir, events } = await fixture()
    const result = spawnSync(process.execPath, [runner], {
      encoding: 'utf8',
      env: {
        ...process.env,
        EVENTS: events,
        PATH: `${join(dir, 'bin')}:${process.env.PATH}`,
        WORKSPACE_CHECK_CONCURRENCY: '2',
      },
    })

    expect(result.status).toBe(1)
    expect(result.stdout).toContain('running 3 checks, up to 2 at a time:')
    expect(result.stderr).toContain('1 of 3 checks failed')

    const entries = (await readFile(events, 'utf8')).trim().split('\n').map(JSON.parse)
    expect(entries.filter(entry => entry.event === 'start')).toHaveLength(3)
    expect(entries.filter(entry => entry.event === 'end')).toHaveLength(3)

    let active = 0
    let maxActive = 0
    for (const entry of entries) {
      active += entry.event === 'start' ? 1 : -1
      maxActive = Math.max(maxActive, active)
    }
    expect(maxActive).toBeLessThanOrEqual(2)
  })
})
