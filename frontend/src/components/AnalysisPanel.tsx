import { useEffect, useMemo, useRef, useState } from 'react'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, apiGet } from '../lib/api'
import {
  QUERY_KEYS,
  cancelStaleAnalysis,
  canonicalQuery,
  isColdStartError,
  isNotFoundError,
  isPreparingError,
  isRateLimitError,
} from '../lib/queries'
import type { AnalysisParams } from '../lib/urlParams'
import type { FieldError } from '../lib/validation'
import type {
  EventsResponse,
  HazardItem,
  RegionStatusResponse,
  SummaryResponse,
  TimeseriesResponse,
} from '../lib/types'
import {
  CaveatBanner,
  ColdStartCard,
  DataQualityPanel,
  EmptyPrompt,
  EmptyState,
  ErrorCard,
  PreparingCard,
  ProgressBar,
  RateLimitBanner,
  Skeleton,
} from './states'
import { SummaryCard } from './SummaryCard'
import { TimeSeriesChart, type ChartMetric } from './TimeSeriesChart'
import { EventsTable } from './EventsTable'
import { Methodology } from './Methodology'

/** Orchestrates the analysis fetches and renders exactly one of the documented
 * state branches (docs/03 §5): idle → loading → success | empty | partial |
 * data-quality | preparing | rate-limited | error. Query keys come from the
 * canonical URL params, so links are shareable and stale responses abort. */

const PAGE_SIZE = 25
const PREPARE_POLL_MS = 5_000
const PREPARE_POLL_MAX_FAILURES = 13 // ≈ 60 s of CF-05 polling, then manual
const BACKFILL_POLL_MS = 5_000
const BACKFILL_POLL_WINDOW_MS = 120_000 // CF-02: 24 polls = 2 minutes

function messageOf(error: unknown): string {
  if (error instanceof ApiError) return error.message
  return 'Something went wrong while loading the analysis.'
}

function traceOf(error: unknown): string | null {
  return error instanceof ApiError ? (error.traceId ?? null) : null
}

function fieldErrorsOf(error: unknown): FieldError[] | undefined {
  if (error instanceof ApiError && error.details?.field) {
    return [
      {
        field: error.details.field,
        reason: error.details.reason ?? error.message,
      },
    ]
  }
  return undefined
}

/** Region polling while the backfill runs (CF-02 step 5). */
function backfillInterval(query: { state: { data?: RegionStatusResponse } }): number | false {
  const data = query.state.data
  if (!data || data.status !== 'backfilling') return false
  const created = Date.parse(data.created_at ?? '')
  const age = Number.isNaN(created) ? 0 : Date.now() - created
  return age < BACKFILL_POLL_WINDOW_MS ? BACKFILL_POLL_MS : false
}

function prepareInterval(query: {
  state: { error: unknown; fetchFailureCount: number }
}): number | false {
  if (!isPreparingError(query.state.error)) return false
  return query.state.fetchFailureCount < PREPARE_POLL_MAX_FAILURES ? PREPARE_POLL_MS : false
}

export interface AnalysisPanelProps {
  params: AnalysisParams
  paramErrors: FieldError[]
  hazard: HazardItem | undefined
  onReset: () => void
  onClearRegion: () => void
  onRetryRegion?: (() => void) | null
  onBusyChange?: (busy: boolean) => void
}

export function AnalysisPanel({
  params,
  paramErrors,
  hazard,
  onReset,
  onClearRegion,
  onRetryRegion,
  onBusyChange,
}: AnalysisPanelProps) {
  const queryClient = useQueryClient()
  const [page, setPage] = useState(1)
  const [metric, setMetric] = useState<ChartMetric>('heatwave_days')
  const [hasLoaded, setHasLoaded] = useState(false)

  const regionId = params.regionId
  const blocked = paramErrors.length > 0
  const canonical = useMemo(() => canonicalQuery(params), [params])

  const statusQuery = useQuery({
    queryKey: QUERY_KEYS.region(regionId ?? 'unknown'),
    queryFn: ({ signal }) => apiGet<RegionStatusResponse>(`/regions/${regionId}`, {}, signal),
    enabled: Boolean(regionId) && !blocked,
    staleTime: 0,
    refetchInterval: backfillInterval,
  })

  const analysisEnabled = Boolean(regionId) && !blocked && statusQuery.data?.status === 'ready'

  const timeseriesQuery = useQuery({
    queryKey: QUERY_KEYS.timeseries(canonical),
    queryFn: ({ signal }) => apiGet<TimeseriesResponse>('/analysis/timeseries', canonical, signal),
    enabled: analysisEnabled,
    refetchInterval: prepareInterval,
  })
  const summaryQuery = useQuery({
    queryKey: QUERY_KEYS.summary(canonical),
    queryFn: ({ signal }) => apiGet<SummaryResponse>('/analysis/summary', canonical, signal),
    enabled: analysisEnabled,
    refetchInterval: prepareInterval,
  })
  const eventsQuery = useQuery({
    queryKey: QUERY_KEYS.events(canonical, page),
    queryFn: ({ signal }) =>
      apiGet<EventsResponse>(
        '/analysis/events',
        { ...canonical, page, page_size: PAGE_SIZE },
        signal,
      ),
    enabled: analysisEnabled,
    refetchInterval: prepareInterval,
    placeholderData: keepPreviousData,
  })

  // CF-13: abandoning a param set aborts its in-flight analysis requests. The
  // latest canonical params are read from a ref, so the fetch that starts for
  // the NEW params (same commit) is never cancelled by this cleanup.
  const canonicalRef = useRef(canonical)
  canonicalRef.current = canonical
  useEffect(
    () => () => cancelStaleAnalysis(queryClient, canonicalRef.current),
    [canonical, queryClient],
  )
  useEffect(() => () => cancelStaleAnalysis(queryClient), [queryClient])
  useEffect(() => setPage(1), [canonical])
  useEffect(() => setHasLoaded(false), [regionId])
  useEffect(() => {
    if (timeseriesQuery.isSuccess && summaryQuery.isSuccess) setHasLoaded(true)
  }, [timeseriesQuery.isSuccess, summaryQuery.isSuccess])

  // Controls lock only for the *first* analysis load (docs/03 §5); after that
  // param changes are stale-while-revalidate so rapid toggles stay possible.
  const firstLoad =
    !hasLoaded &&
    Boolean(regionId) &&
    !blocked &&
    (statusQuery.isPending ||
      (analysisEnabled && (timeseriesQuery.isPending || summaryQuery.isPending)))
  useEffect(() => onBusyChange?.(firstLoad), [firstLoad, onBusyChange])

  function retryAll() {
    void Promise.all([timeseriesQuery.refetch(), summaryQuery.refetch(), eventsQuery.refetch()])
  }

  // -- blocked / no region ------------------------------------------------
  if (blocked) {
    return (
      <ErrorCard
        title="Invalid analysis parameters"
        message="The link or query you followed has parameters the analyzer rejects. Nothing was requested from the server."
        fieldErrors={paramErrors}
        onSecondary={onReset}
        secondaryLabel="Reset to defaults"
      />
    )
  }
  if (!regionId) return <EmptyPrompt />

  // -- region status ------------------------------------------------------
  if (statusQuery.isPending) return <Skeleton label="Loading region status" />
  if (statusQuery.isError) {
    const error = statusQuery.error
    if (isNotFoundError(error)) {
      return (
        <ErrorCard
          title="Region not found"
          message="This link points at a region the server does not recognize (it may have been reset)."
          traceId={traceOf(error)}
          onSecondary={onClearRegion}
          secondaryLabel="Choose a region"
        />
      )
    }
    if (isColdStartError(error)) {
      return <ColdStartCard onRetry={() => void statusQuery.refetch()} />
    }
    return (
      <ErrorCard
        title="Could not load the region"
        message={messageOf(error)}
        traceId={traceOf(error)}
        onRetry={() => void statusQuery.refetch()}
      />
    )
  }

  const status = statusQuery.data
  const regionName = status.name

  if (status.status === 'failed') {
    return (
      <ErrorCard
        title="Could not load weather data for this region"
        message="The backfill for this region failed, so no honest analysis can be produced. Re-submit the same region to retry the fetch."
        traceId={null}
        onRetry={onRetryRegion ? onRetryRegion : () => void statusQuery.refetch()}
        retryLabel={onRetryRegion ? 'Retry backfill' : 'Check status'}
      />
    )
  }
  if (status.status === 'backfilling') {
    return (
      <PreparingCard name={regionName || undefined} onRetry={() => void statusQuery.refetch()} />
    )
  }

  // -- analysis -----------------------------------------------------------
  const timeseries = timeseriesQuery.data
  const summary = summaryQuery.data
  const hasData = Boolean(
    timeseriesQuery.isSuccess && summaryQuery.isSuccess && timeseries && summary,
  )
  const error = timeseriesQuery.error ?? summaryQuery.error ?? eventsQuery.error
  const rateLimitError = [
    timeseriesQuery.error,
    summaryQuery.error,
    eventsQuery.error,
    timeseriesQuery.failureReason,
    summaryQuery.failureReason,
    eventsQuery.failureReason,
  ].find((candidate): candidate is ApiError => isRateLimitError(candidate))
  const anyFetching =
    timeseriesQuery.isFetching || summaryQuery.isFetching || eventsQuery.isFetching

  if (!hasData) {
    if (rateLimitError) {
      return (
        <div>
          <RateLimitBanner
            seconds={rateLimitError.retryAfterSeconds ?? 60}
            autoRetry={anyFetching}
            onRetry={retryAll}
          />
          <Skeleton />
        </div>
      )
    }
    if (error && isPreparingError(error)) {
      return (
        <PreparingCard
          name={regionName || undefined}
          note="This range needs data the server is still fetching. Polling every 5 s — you can also check manually."
          onRetry={retryAll}
        />
      )
    }
    if (error)
      return (
        <ErrorState
          error={error}
          onRetry={retryAll}
          onReset={onReset}
          onClearRegion={onClearRegion}
        />
      )
    return <Skeleton />
  }
  if (!timeseries || !summary) return <Skeleton />

  const caveats = summary.caveats
  const suppressed = summary.headline_kind === 'suppressed_low_coverage'
  const totals = summary.stats.totals
  const empty = totals.events === 0 && !suppressed
  const lowCoverage = timeseries.series
    .filter((row) => row.coverage_pct < 80)
    .map((row) => ({ year: row.year, coverage_pct: row.coverage_pct }))
  const shownLowCoverage =
    lowCoverage.length > 0
      ? lowCoverage
      : timeseries.series
          .filter((row) => row.coverage_pct < 90)
          .map((row) => ({ year: row.year, coverage_pct: row.coverage_pct }))
  const hazardLabel = hazard?.label ?? 'Hazard'

  return (
    <div className="panel-body">
      {timeseriesQuery.isFetching && <ProgressBar />}
      {rateLimitError && (
        <RateLimitBanner
          seconds={rateLimitError.retryAfterSeconds ?? 60}
          autoRetry={anyFetching}
          onRetry={retryAll}
        />
      )}
      <CaveatBanner caveats={caveats} />
      {suppressed ? (
        <DataQualityPanel headline={summary.headline} lowCoverage={shownLowCoverage} />
      ) : empty ? (
        <EmptyState
          headline={summary.headline}
          startYear={params.startYear}
          endYear={params.endYear}
          hazardLabel={hazardLabel}
          definition={
            hazard?.description ??
            '3+ consecutive days above the regional 95th-percentile threshold.'
          }
        />
      ) : (
        <SummaryCard
          summary={summary}
          regionLabel={regionName}
          startYear={params.startYear}
          endYear={params.endYear}
        />
      )}
      <TimeSeriesChart
        series={timeseries.series}
        rolling={timeseries.rolling_mean}
        ols={summary.stats.ols}
        metric={metric}
        onMetricChange={setMetric}
        hazardLabel={hazardLabel}
        regionLabel={regionName}
        startYear={params.startYear}
        endYear={params.endYear}
      />
      {!empty && (
        <EventsTable
          items={eventsQuery.data?.items ?? []}
          page={page}
          pageSize={eventsQuery.data?.page_size ?? PAGE_SIZE}
          total={eventsQuery.data?.total ?? 0}
          pending={eventsQuery.isPending}
          hazardLabel={hazardLabel}
          startYear={params.startYear}
          endYear={params.endYear}
          onPageChange={setPage}
        />
      )}
      {timeseries.region.kind === 'bbox' && (
        <p className="grid-note" data-testid="grid-caveat">
          Based on a 3×3 grid average across the selected box{' '}
        </p>
      )}
      <Methodology hazard={hazard} meta={summary.meta} />
    </div>
  )
}

function ErrorState({
  error,
  onRetry,
  onReset,
  onClearRegion,
}: {
  error: unknown
  onRetry: () => void
  onReset: () => void
  onClearRegion: () => void
}) {
  if (isNotFoundError(error)) {
    return (
      <ErrorCard
        title="Region not found"
        message="This region no longer exists on the server. Choose a region again to continue."
        traceId={traceOf(error)}
        onSecondary={onClearRegion}
        secondaryLabel="Choose a region"
      />
    )
  }
  if (error instanceof ApiError && error.status === 422) {
    return (
      <ErrorCard
        title="Invalid analysis parameters"
        message={error.message}
        fieldErrors={fieldErrorsOf(error)}
        traceId={traceOf(error)}
        onSecondary={onReset}
        secondaryLabel="Reset to defaults"
      />
    )
  }
  if (isColdStartError(error)) {
    return <ColdStartCard onRetry={onRetry} />
  }
  return (
    <ErrorCard
      title="Analysis failed"
      message={messageOf(error)}
      fieldErrors={fieldErrorsOf(error)}
      traceId={traceOf(error)}
      code={error instanceof ApiError ? error.code : undefined}
      onRetry={onRetry}
    />
  )
}
