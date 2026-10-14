import type { HazardItem, ResponseMeta } from '../lib/types'

/** Methodology tooltip (P4) — the honest fine print behind every chart:
 * threshold definition, streak rules, attribution, honesty gates and the
 * exact versions the numbers came from (EC-19, DEC-03/07/21, NFR-05). */

export interface MethodologyProps {
  hazard: HazardItem | undefined
  meta: ResponseMeta | null
}

export function Methodology({ hazard, meta }: MethodologyProps) {
  return (
    <details className="card methodology" data-testid="methodology">
      <summary title="How the analyzer decides what counts as a heatwave">
        Methodology &amp; data notes
      </summary>
      <dl>
        <dt>Definition</dt>
        <dd>{hazard?.description ?? 'Hazard definition loaded from the API registry.'}</dd>
        <dt>Threshold</dt>
        <dd>
          Regional 95th percentile of daily maximum temperature for each calendar day, using a
          ±15-day window over the {meta?.baseline ?? '1991–2020'} baseline. Days with no usable
          threshold are unassessable, never guessed.
        </dd>
        <dt>Streak rule</dt>
        <dd>
          {hazard?.min_days ?? 3}+ consecutive days <em>strictly above</em> the threshold count as
          one event. A missing, outlier or degraded day breaks the streak.
        </dd>
        <dt>Year attribution</dt>
        <dd>
          An event counts in the year it starts; its days are split across the calendar years it
          touches, so a Dec–Jan event contributes days to both years but one event to the start
          year.
        </dd>
        <dt>Honesty gates</dt>
        <dd>
          Years under 80% valid-day coverage are excluded from trend statistics (still plotted). %
          change needs at least 6 years; p ≥ 0.05 damps the headline wording; under 60% good years
          the trend claim is suppressed entirely.
        </dd>
        <dt>Versions</dt>
        <dd>
          {meta
            ? `Algorithm ${meta.algo_version}, data ${meta.data_version}, source ${meta.source}, generated ${meta.generated_at}.`
            : 'Version metadata appears once a result loads.'}
        </dd>
      </dl>
    </details>
  )
}
