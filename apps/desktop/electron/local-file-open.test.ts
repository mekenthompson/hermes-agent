import assert from 'node:assert/strict'

import { test } from 'vitest'

import { decideLocalFileOpenAction } from './local-file-open'

test('Windows archive artifacts reveal in Explorer instead of shell.openPath', () => {
  assert.equal(decideLocalFileOpenAction('C:\\Users\\kenth\\Downloads\\vite-1.0.0.tgz', 'win32'), 'reveal')
  assert.equal(decideLocalFileOpenAction('C:\\cache\\pkg.tar.gz', 'win32'), 'reveal')
  assert.equal(decideLocalFileOpenAction('C:\\out\\bundle.zip', 'win32'), 'reveal')
})

test('Windows ordinary files still open with the OS association', () => {
  assert.equal(decideLocalFileOpenAction('C:\\Users\\kenth\\Downloads\\report.md', 'win32'), 'open')
  assert.equal(decideLocalFileOpenAction('C:\\Users\\kenth\\Pictures\\chart.png', 'win32'), 'open')
})

test('macOS and Linux keep openPath for archives so Finder/file associations still work', () => {
  assert.equal(decideLocalFileOpenAction('/tmp/pkg.tgz', 'darwin'), 'open')
  assert.equal(decideLocalFileOpenAction('/tmp/pkg.zip', 'linux'), 'open')
})
