import crypto from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, describe, expect, it } from 'vitest'

import {
  looksBinary,
  MEDIA_MIME_TYPES,
  PREVIEW_LANGUAGE_BY_EXT,
  previewFileMetadata,
  TEXT_PREVIEW_MAX_BYTES
} from './preview-file-metadata'

const temporaryFiles: string[] = []

afterEach(() => {
  for (const file of temporaryFiles.splice(0)) {
    fs.rmSync(file, { force: true })
  }
})

function tempFile(contents: Buffer): string {
  const file = path.join(os.tmpdir(), `hermes-preview-metadata-${crypto.randomUUID()}`)

  fs.writeFileSync(file, contents)
  temporaryFiles.push(file)

  return file
}

describe('preview file metadata', () => {
  it('keeps the supported media and language mappings available', () => {
    expect(MEDIA_MIME_TYPES['.mp4']).toBe('video/mp4')
    expect(PREVIEW_LANGUAGE_BY_EXT['.ts']).toBe('typescript')
  })

  it('identifies binary data while permitting common text whitespace', () => {
    expect(looksBinary(Buffer.from('line one\nline two\t'))).toBe(false)
    expect(looksBinary(Buffer.from([0x41, 0, 0x42]))).toBe(true)
    expect(looksBinary(Buffer.from([0x41, 0x01, ...Array(20).fill(0x41)]))).toBe(false)
  })

  it('reports size and binary status, but does not scan image bytes', () => {
    const textPath = tempFile(Buffer.from('plain text'))
    const imagePath = tempFile(Buffer.from([0, 0, 0]))
    expect(previewFileMetadata(textPath, 'text/plain')).toEqual({ binary: false, byteSize: 10, large: false })
    expect(previewFileMetadata(imagePath, 'image/png')).toEqual({ binary: false, byteSize: 3, large: false })
  })

  it('keeps the large-file threshold and treats unreadable paths as empty metadata', () => {
    const file = tempFile(Buffer.alloc(TEXT_PREVIEW_MAX_BYTES + 1, 0x61))
    expect(previewFileMetadata(file, 'text/plain')).toEqual({
      binary: false,
      byteSize: TEXT_PREVIEW_MAX_BYTES + 1,
      large: true
    })
    expect(previewFileMetadata(`${file}.missing`, 'text/plain')).toEqual({ binary: false, byteSize: 0, large: false })
  })
})
