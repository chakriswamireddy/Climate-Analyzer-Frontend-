/** Explicit UI state branches for the analysis panel (docs/03 §5).
 * Every branch is a named component so tests can target it directly and no
 * state ever falls through to an implicit undefined render. */

import { useEffect, useRef, useState, type ReactNode } from 'react'
import { reportClientError } from '../lib/api'
import type { FieldError } from '../lib/validation'

export function Skeleton({ label = 'Loading analysis' }: { label?: string }) {
  return (
    <div className="skeleton" role="status" aria-label={label} data-testid="analysis-skeleton">
      <div className="skeleton__block skeleton__block--head" />
      <div className="skeleton__block skeleton__block--chart" />
      <div className="skeleton__block skeleton__block--table" />
      <span className="sr-only">{label}…</span>
    </div>
  )
}

export function ProgressBar() {
  return (
    <div className="progress" role="progressbar" aria-label="Refreshing results">
      <span className="progress__bar" />
    </div>
  )
}

function CopyButton({ value }: { value: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      type="button"
      className="btn btn--ghost btn--small"
      onClick={() => {
        const clipboard = navigator.clipboard
        if (!clipboard) return
        clipboard
          .writeText(value)
          .then(() => {
            setCopied(true)
            setTimeout(() => setCopied(false), 1500)
          })
          .catch(() => undefined)
      }}
    >
      {copied ? 'Copied' : 'Copy'}
    </button>
  )
}

export interface ErrorCardProps {
  title: string
  message: string
  fieldErrors?: FieldError[]
  traceId?: string | null
  onRetry?: () => void
  retryLabel?: string
  onSecondary?: () => void
  secondaryLabel?: string
  code?: string
}

export function ErrorCard({
  title,
  message,
  fieldErrors,
  traceId,
  onRetry,
  retryLabel = 'Try again',
  onSecondary,
  secondaryLabel,
  code,
}: ErrorCardProps) {
  return (
    <section className="card card--error" role="alert" data-testid="error-card">
      <h2>{title}</h2>
      <p>{message}</p>
      {fieldErrors && fieldErrors.length > 0 && (
        <ul className="error-list" data-testid="error-fields">
          {fieldErrors.map((error) => (
            <li key={`${error.field}:${error.reason}`}>
              <code>{error.field}</code> — {error.reason}
            </li>
          ))}
        </ul>
      )}
      {traceId && (
        <p className="trace">
          Trace id: <code data-testid="trace-id">{traceId}</code>
          <CopyButton value={traceId} />
        </p>
      )}
      <div className="actions">
        {onRetry && (
          <button type="button" className="btn btn--primary" onClick={onRetry}>
            {retryLabel}
          </button>
        )}
        {onSecondary && (
          <button type="button" className="btn" onClick={onSecondary}>
            {secondaryLabel}
          </button>
        )}
        <button
          type="button"
          className="btn btn--ghost"
          onClick={() => reportClientError({ message, code, trace_id: traceId })}
        >
          Report
        </button>
      </div>
    </section>
  )
}

export function FatalConfigBanner({ onReload }: { onReload: () => void }) {
  return (
    <section className="card card--error" role="alert" data-testid="fatal-banner">
      <h2>Unable to load app configuration</h2>
      <p>The hazard registry could not be fetched, so the analyzer cannot start safely.</p>
      <div className="actions">
        <button type="button" className="btn btn--primary" onClick={onReload}>
          Reload
        </button>
      </div>
    </section>
  )
}

const COLD_START_POLL_MS = 5_000
const COLD_START_MAX_POLLS = 18 // ≈ 90 s of auto-checking before manual-only

/** Free-tier cold start (DEC-14, 05 §P6): the deployed API sleeps after idle, so
 * the first call can fail to connect or answer 503 for up to ~60 s. Instead of a
 * dead-end error the UI names what is happening and keeps checking on its own. */
export function ColdStartCard({ note, onRetry }: { note?: string; onRetry?: () => void }) {
  const [polls, setPolls] = useState(0)
  const retryRef = useRef(onRetry)
  retryRef.current = onRetry
  const exhausted = polls >= COLD_START_MAX_POLLS

  useEffect(() => {
    if (exhausted || !retryRef.current) return
    const timer = setTimeout(() => {
      setPolls((value) => value + 1)
      retryRef.current?.()
    }, COLD_START_POLL_MS)
    return () => clearTimeout(timer)
  }, [polls, exhausted])

  return (
    <section className="card" role="status" data-testid="cold-start-card">
      <h2>{exhausted ? 'The API is still not answering' : 'Waking up the API…'}</h2>
      <p>
        {exhausted
          ? 'The free-tier service did not respond within 90 s. It may still be starting (cold start) or be temporarily down — you can check again.'
          : (note ??
            'This app runs on a free tier, which sleeps after a period of inactivity. The first request can take up to a minute. Checking again automatically…')}
      </p>
      <div className="actions">
        <button
          type="button"
          className="btn btn--primary"
          onClick={() => {
            setPolls(0)
            retryRef.current?.()
          }}
        >
          Try again
        </button>
      </div>
    </section>
  )
}

export function EmptyPrompt() {
  return (
    <section className="card card--empty" data-testid="empty-prompt">
      <h2>Select a region to analyze</h2>
      <p>
        Pick one of three ways above: search for a place, choose a curated preset, or enter a
        latitude/longitude box by hand.
      </p>
      <ul className="hint-list">
        <li>Search — city or town name (server-side geocoding, rate-limited)</li>
        <li>Presets — 25 Indian cities and 12 states</li>
        <li>Coordinates — a custom box up to 10° per side</li>
      </ul>
    </section>
  )
}

export function EmptyState({
  headline,
  startYear,
  endYear,
  hazardLabel,
  definition,
}: {
  headline: string
  startYear: number
  endYear: number
  hazardLabel: string
  definition: string
}) {
  return (
    <section className="card card--empty" data-testid="empty-state">
      <h2>{headline}</h2>
      <p>
        An empty result is a result: the detector found no qualifying {hazardLabel.toLowerCase()}{' '}
        events between {startYear} and {endYear}.
      </p>
      <p className="definition">
        <strong>Definition used:</strong> {definition}
      </p>
    </section>
  )
}

export function DataQualityPanel({
  headline,
  lowCoverage,
}: {
  headline: string
  lowCoverage: { year: number; coverage_pct: number }[]
}) {
  return (
    <section className="card card--warn" data-testid="data-quality">
      <h2>Trend not reported — data quality too low</h2>
      <p>{headline}</p>
      <p>
        Most years in this range have under 80% valid-day coverage, so any trend statement would
        overclaim (NFR-05). Per-year coverage:
      </p>
      <ul className="coverage-list">
        {lowCoverage.map((row) => (
          <li key={row.year}>
            {row.year}: {row.coverage_pct.toFixed(1)}% valid days
          </li>
        ))}
      </ul>
    </section>
  )
}

export function PreparingCard({
  name,
  note,
  lastError,
  pending,
  onRetry,
}: {
  name?: string
  note?: string
  lastError?: string | null
  pending?: boolean
  onRetry?: () => void
}) {
  return (
    <section className="card" role="status" data-testid="preparing-card">
      <h2>{name ? `Preparing data for ${name}…` : 'Preparing historical data…'}</h2>
      <p>
        {note ??
          'First-time analysis can take ~30–60 s while thresholds and daily rows are computed. You can leave this page — the work continues on the server.'}
      </p>
      {lastError && (
        <p className="definition" data-testid="preparing-last-error">
          <strong>Last attempt failed:</strong> {lastError} — the server retries automatically.
        </p>
      )}
      <div className="actions">
        <button type="button" className="btn" onClick={onRetry} disabled={pending}>
          {pending ? 'Checking…' : 'Check again'}
        </button>
      </div>
    </section>
  )
}

export function RateLimitBanner({
  seconds,
  autoRetry,
  onRetry,
}: {
  seconds: number
  autoRetry: boolean
  onRetry: () => void
}) {
  const [left, setLeft] = useState(seconds)
  useEffect(() => setLeft(seconds), [seconds])
  useEffect(() => {
    if (left <= 0) return
    const timer = setTimeout(() => setLeft((value) => value - 1), 1000)
    return () => clearTimeout(timer)
  }, [left])

  const text = autoRetry
    ? left > 0
      ? `Rate limit reached — retrying in ${left}s`
      : 'Rate limit reached — retrying…'
    : left > 0
      ? `Rate limit reached — try again in ${left}s`
      : 'Rate limit reached — try again shortly'

  return (
    <div className="banner banner--warn" role="status" data-testid="rate-limit-banner">
      <span>{text}</span>
      {!autoRetry && (
        <button type="button" className="btn btn--small" onClick={onRetry}>
          Try now
        </button>
      )}
    </div>
  )
}

export function CaveatBanner({ caveats }: { caveats: string[] }) {
  if (caveats.length === 0) return null
  return (
    <div className="banner banner--warn" role="status" data-testid="caveat-banner">
      <strong>Read with care:</strong>
      <ul>
        {caveats.map((caveat) => (
          <li key={caveat}>{caveat}</li>
        ))}
      </ul>
    </div>
  )
}

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="card">
      <h2>{title}</h2>
      {children}
    </section>
  )
}
