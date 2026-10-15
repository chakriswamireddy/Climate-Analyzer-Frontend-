/** Status-poll cadence for a running region backfill (docs/03 CF-02 step 5).
 *
 * The 2-minute budget is keyed on when *this client* first saw `backfilling`,
 * never on `created_at`: a region created hours ago (page reload, shared
 * link, or the card's own "you can leave this page" invitation) must still
 * poll. After the fast window the poller *degrades* to a slow interval
 * instead of stopping — a poller that stops dead is what froze the preparing
 * card forever while the server happily finished the backfill. */

import type { RegionStatusResponse } from './types'

export const BACKFILL_POLL_WINDOW_MS = 120_000 // 24 fast polls = 2 minutes
export const BACKFILL_POLL_SLOW_MS = 15_000 // after the window: degraded, never off

export function backfillPollMs(
  data: Pick<RegionStatusResponse, 'status' | 'poll_after_seconds'> | undefined,
  observedAt: number | null,
  now: number,
): number | false {
  if (!data || data.status !== 'backfilling') return false
  const observed = observedAt ?? now
  const fastMs = Math.max(1, data.poll_after_seconds ?? 5) * 1000
  return now - observed < BACKFILL_POLL_WINDOW_MS ? fastMs : BACKFILL_POLL_SLOW_MS
}

/** Region-status `last_error` is `{code, message}` from the API (docs/02 §6.1). */
export function lastErrorMessage(lastError: unknown): string | null {
  if (!lastError) return null
  if (typeof lastError === 'string') return lastError || null
  if (typeof lastError === 'object' && 'message' in lastError) {
    const message = (lastError as { message?: unknown }).message
    if (typeof message === 'string' && message) return message
  }
  return null
}
