import { useCallback, useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { apiGet, apiPost } from './lib/api'
import { QUERY_KEYS } from './lib/queries'
import {
  DEFAULT_ANALYSIS_PARAMS,
  parseAnalysisParams,
  validateParams,
  writeAnalysisParams,
  type AnalysisParams,
} from './lib/urlParams'
import type { FieldError } from './lib/validation'
import type { HazardsResponse, RegionCreateBody, RegionCreateResponse } from './lib/types'
import { HazardSelector } from './components/HazardSelector'
import { RangePicker } from './components/RangePicker'
import { RegionSelector } from './components/RegionSelector'
import { AnalysisPanel } from './components/AnalysisPanel'
import { FatalConfigBanner } from './components/states'

/** App shell: URL params → react-query keys → controls + analysis panel.
 * The query string is the single source of truth for shareable links
 * (docs/02 §7.4); every commit rewrites it with `replaceState`. */

export default function App() {
  const queryClient = useQueryClient()
  const initial = useMemo(() => parseAnalysisParams(window.location.search), [])
  const [parseErrors, setParseErrors] = useState<FieldError[]>(initial.errors)
  const [params, setParams] = useState<AnalysisParams>(initial.params)
  const [lastSpec, setLastSpec] = useState<RegionCreateBody | null>(null)
  const [busy, setBusy] = useState(false)

  const rangeErrors = useMemo(() => validateParams(params), [params])

  const hazardsQuery = useQuery({
    queryKey: QUERY_KEYS.hazards,
    queryFn: ({ signal }) => apiGet<HazardsResponse>('/hazards', {}, signal),
    staleTime: 60 * 60_000,
  })

  const write = useCallback((next: AnalysisParams) => {
    setParams(next)
    writeAnalysisParams(next)
  }, [])

  // Any param commit means the user is now driving the state, so stale
  // parse errors from a hand-edited URL are dropped (range errors re-derive).
  const commit = useCallback((patch: Partial<AnalysisParams>) => {
    setParseErrors([])
    setParams((previous) => {
      const next = { ...previous, ...patch }
      writeAnalysisParams(next)
      return next
    })
  }, [])

  const resetAll = useCallback(() => {
    setParseErrors([])
    write({ ...DEFAULT_ANALYSIS_PARAMS })
  }, [write])

  const clearRegion = useCallback(() => commit({ regionId: null }), [commit])

  const handleCreated = useCallback(
    (regionId: string, _name: string, body: RegionCreateBody) => {
      setLastSpec(body)
      commit({ regionId })
    },
    [commit],
  )

  const retryBackfill = useCallback(async () => {
    if (!lastSpec) return
    try {
      const response = await apiPost<RegionCreateResponse>('/regions', lastSpec)
      commit({ regionId: response.region_id })
      await queryClient.invalidateQueries({ queryKey: QUERY_KEYS.region(response.region_id) })
    } catch {
      // still failed — the panel keeps showing the failed-state card
    }
  }, [lastSpec, commit, queryClient])

  const hazards = hazardsQuery.data
  const unknownHazard: FieldError[] =
    hazards && !hazards.items.some((item) => item.id === params.hazard)
      ? [
          {
            field: 'hazard',
            reason: `unsupported hazard "${params.hazard}"; supported: ${hazards.items
              .map((item) => item.id)
              .join(', ')}`,
          },
        ]
      : []
  const paramErrors = [...parseErrors, ...rangeErrors, ...unknownHazard]

  const hazard = hazards?.items.find((item) => item.id === params.hazard)

  if (hazardsQuery.isError) {
    return (
      <div className="app">
        <Header />
        <FatalConfigBanner onReload={() => window.location.reload()} />
      </div>
    )
  }

  return (
    <div className="app">
      <Header />
      {!hazards && (
        <p className="hint" role="status">
          Loading app configuration…
        </p>
      )}
      {hazards && (
        <section className="controls" aria-label="Analysis controls">
          <div className="controls__row">
            <HazardSelector
              items={hazards.items}
              defaultId={hazards.default}
              value={params.hazard}
              onChange={(hazardId) => commit({ hazard: hazardId })}
              disabled={busy}
            />
            <RangePicker
              startYear={params.startYear}
              endYear={params.endYear}
              onChange={(range) => commit(range)}
              disabled={busy}
            />
            <div className="field field--action">
              <span className="field__label" aria-hidden="true">
                &nbsp;
              </span>
              <button
                type="button"
                className="btn"
                hidden={!busy}
                onClick={clearRegion}
                data-testid="cancel-analysis"
              >
                Cancel
              </button>
            </div>
          </div>
          <RegionSelector onCreated={handleCreated} />
        </section>
      )}
      {hazards && (
        <AnalysisPanel
          params={params}
          paramErrors={paramErrors}
          hazard={hazard}
          onReset={resetAll}
          onClearRegion={clearRegion}
          onRetryRegion={lastSpec ? () => void retryBackfill() : null}
          onBusyChange={setBusy}
        />
      )}
    </div>
  )
}

function Header() {
  return (
    <header className="header">
      <h1>Climate Hazard Trend Analyzer</h1>
      <p className="tagline">
        Historical heatwave frequency, duration and intensity for any city, state or box — from the
        Open-Meteo archive, with the caveats always on screen.
      </p>
    </header>
  )
}
