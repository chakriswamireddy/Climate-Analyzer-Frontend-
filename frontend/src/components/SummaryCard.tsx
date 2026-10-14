import type { SummaryResponse } from '../lib/types'

/** FR-07 / DEC-07: render the server-computed headline plus the honesty
 * context (significance, coverage, windows). Nothing is re-derived here. */

function formatRelative(iso: string): string {
  const at = Date.parse(iso)
  if (Number.isNaN(at)) return ''
  const seconds = Math.max(0, Math.round((Date.now() - at) / 1000))
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`
  return new Date(at).toISOString().slice(11, 16) + ' UTC'
}

function signedPercent(value: number | null): string | null {
  if (value === null) return null
  const rounded = Math.round(value)
  return `${rounded > 0 ? '+' : ''}${rounded}%`
}

export interface SummaryCardProps {
  summary: SummaryResponse
  regionLabel: string
  startYear: number
  endYear: number
}

export function SummaryCard({ summary, regionLabel, startYear, endYear }: SummaryCardProps) {
  const { stats, meta } = summary
  const ols = stats.ols
  const pctDays = signedPercent(stats.pct_change_days)
  const totals = stats.totals
  const significant = ols?.p_value != null && ols.p_value < 0.05

  return (
    <section className="card summary" data-testid="summary-card">
      <p className="summary__context">
        {regionLabel ? `${regionLabel} · ` : ''}
        {startYear}–{endYear}
      </p>
      <h2 data-testid="headline">{summary.headline}</h2>
      <ul className="chips">
        {pctDays && (
          <li className="chip" data-testid="pct-chip">
            {pctDays} heatwave days (first 3 vs last 3 years)
          </li>
        )}
        {ols && (
          <li className="chip" data-testid="ols-chip">
            OLS {ols.slope_per_decade > 0 ? '+' : ''}
            {ols.slope_per_decade} days/decade
            {ols.p_value != null &&
              ` · ${significant ? 'significant' : 'not significant'} (p=${ols.p_value})`}
          </li>
        )}
        <li className="chip">
          {totals.events} events · {totals.days} days · longest {totals.longest_event_days} days
        </li>
        {stats.baseline_window && stats.recent_window && (
          <li className="chip">
            {stats.baseline_window.mean_days} days/yr in {stats.baseline_window.start_year}–
            {stats.baseline_window.end_year} → {stats.recent_window.mean_days} days/yr in{' '}
            {stats.recent_window.start_year}–{stats.recent_window.end_year}
          </li>
        )}
        <li className="chip chip--muted">Updated {formatRelative(meta.generated_at)}</li>
      </ul>
      <p className="meta-line">
        Source: {meta.source} · threshold baseline {meta.baseline} · algorithm v{meta.algo_version}{' '}
        · data {meta.data_version}
      </p>
    </section>
  )
}
