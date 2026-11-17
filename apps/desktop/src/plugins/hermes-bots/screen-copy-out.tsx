/**
 * "Copy from screen": the Bot Screen's text clipboard onto the viewer's own clipboard.
 *
 * Xvnc never sends cut text to viewers (watchers must not receive the holder's clipboard), so this is a
 * request, not a stream: one `display.clipboard.read` RPC per click, answered by the gateway only to the
 * viewer the lease names. Nothing polls, nothing is stored, and the text is never logged.
 */

import { Button, Codicon, Tip } from '@hermes/plugin-sdk'
import type { DisplayClipboardReadResult } from '@hermes/plugin-sdk'
import { useCallback, useEffect, useRef, useState } from 'react'

import { useBots } from './i18n'
import { displayRequest } from './screen-connection'
import type { RosterRow } from './types'

export type CopyOutState = 'busy' | 'copied' | 'empty' | 'failed' | 'idle'

/** How long the "Copied" confirmation stays on the toolbar. */
const FEEDBACK_MS = 2500

/** Fetch the screen's clipboard text for `viewerId` and write it to `navigator.clipboard`. Resolves `copied`,
 *  or `empty` when nothing is selected on the screen (the user's clipboard is then left untouched). */
export async function copyFromScreen(bot: RosterRow, viewerId: string): Promise<'copied' | 'empty'> {
  const { text } = await displayRequest<DisplayClipboardReadResult>(bot, 'display.clipboard.read', {
    viewer_id: viewerId
  })

  if (!text) {
    return 'empty'
  }

  await navigator.clipboard.writeText(text)

  return 'copied'
}

export function useCopyFromScreen(bot: RosterRow, viewerId: string | undefined) {
  const [state, setState] = useState<CopyOutState>('idle')
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => () => void (timer.current && clearTimeout(timer.current)), [])

  // Control moved or the stream re-attached under a new viewer id: the previous outcome is stale.
  useEffect(() => setState('idle'), [viewerId])

  const copy = useCallback(async () => {
    if (!viewerId) {
      return
    }

    setState('busy')

    let next: CopyOutState

    try {
      next = await copyFromScreen(bot, viewerId)
    } catch {
      next = 'failed'
    }

    setState(next)

    if (timer.current) {
      clearTimeout(timer.current)
    }

    timer.current = setTimeout(() => setState('idle'), FEEDBACK_MS)
  }, [bot, viewerId])

  return { copy, state }
}

const NOTE_KEY = { copied: 'copiedFromScreen', empty: 'copyEmpty', failed: 'copyFailed' } as const

/** The "Copy from screen" action and its outcome text. Rendered only while this window holds the lease: a
 *  watcher never sees it (and the gateway would refuse it anyway). */
export function ScreenCopyOut({
  bot,
  busy,
  live,
  viewerId
}: {
  bot: RosterRow
  busy: boolean
  live: boolean
  viewerId?: string
}) {
  const t = useBots()
  const { copy, state } = useCopyFromScreen(bot, viewerId)
  const note = state === 'idle' || state === 'busy' ? null : t.screen[NOTE_KEY[state]]

  return (
    <>
      {note ? (
        <span className={state === 'failed' ? 'text-red-500' : 'text-muted-foreground'} role="status">
          {note}
        </span>
      ) : null}
      <Tip label={t.screen.copyFromScreenHint}>
        <Button disabled={busy || !live || state === 'busy'} onClick={() => void copy()} size="sm" variant="secondary">
          <Codicon name="copy" /> {t.screen.copyFromScreen}
        </Button>
      </Tip>
    </>
  )
}
