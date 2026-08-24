import path from 'node:path'

// Archive/compressed files that Windows/Electron mishandles when passed to
// shell.openPath(): Chromium starts a download loop of UUID-named .tmp files
// into ~/Downloads until the machine locks up (#53170). Reveal in Explorer
// instead. Other platforms keep openPath so Finder/xdg associations still work.
export const ARCHIVE_EXTENSIONS = new Set([
  '.7z',
  '.bz2',
  '.gz',
  '.lz',
  '.lzma',
  '.rar',
  '.tar',
  '.tgz',
  '.xz',
  '.zip',
  '.zst'
])

export type LocalFileOpenAction = 'open' | 'reveal'

export function decideLocalFileOpenAction(
  localPath: string,
  platform: NodeJS.Platform = process.platform
): LocalFileOpenAction {
  if (platform !== 'win32') {
    return 'open'
  }

  const ext = path.win32.extname(String(localPath || '')).toLowerCase()

  return ARCHIVE_EXTENSIONS.has(ext) ? 'reveal' : 'open'
}
