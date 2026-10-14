import { useState, type FormEvent } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { ApiError, apiGet, apiPost } from '../lib/api'
import { QUERY_KEYS } from '../lib/queries'
import { useDebouncedValue } from '../hooks/useDebouncedValue'
import { isValidSearchQuery, validateBbox, type Bbox, type FieldError } from '../lib/validation'
import type {
  PresetsResponse,
  RegionCreateBody,
  RegionCreateResponse,
  SearchResponse,
} from '../lib/types'

/** FR-02 / DEC-05: three ways to pick a region — debounced geocode search
 * (CF-02), curated presets, and a manual bbox form that mirrors V-06 (CF-03).
 * `POST /regions` is idempotent, the submit button is disabled while in
 * flight (CF-10), and failures render inline instead of silently. */

export interface RegionSelectorProps {
  onCreated: (regionId: string, name: string, body: RegionCreateBody) => void
  disabled?: boolean
}

const EMPTY_BBOX = { minLat: '', minLon: '', maxLat: '', maxLon: '', name: '' }

function searchErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 429) {
      return `Too many searches — wait ${error.retryAfterSeconds ?? 30}s and try again.`
    }
    if (error.status === 503) return 'Search unavailable — use manual coordinates.'
    if (error.code === 'VALIDATION_ERROR') return error.message
  }
  return 'Search failed — check your connection, or enter coordinates manually.'
}

function createErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 429) {
      return `Region requests are rate-limited — wait ${error.retryAfterSeconds ?? 60}s and try again.`
    }
    if (error.status === 422) return error.message
    if (error.status >= 500) return 'The server could not register this region — please retry.'
    return error.message
  }
  return 'Could not reach the server — please retry.'
}

export function RegionSelector({ onCreated, disabled }: RegionSelectorProps) {
  const [query, setQuery] = useState('')
  const debouncedQuery = useDebouncedValue(query, 300)
  const [bboxDraft, setBboxDraft] = useState(EMPTY_BBOX)
  const [bboxErrors, setBboxErrors] = useState<FieldError[]>([])

  const searchEnabled = isValidSearchQuery(debouncedQuery)
  const searchQuery = useQuery({
    queryKey: QUERY_KEYS.search(debouncedQuery.trim().toLowerCase()),
    queryFn: ({ signal }) =>
      apiGet<SearchResponse>('/regions/search', { q: debouncedQuery }, signal),
    enabled: searchEnabled,
    staleTime: 60_000,
    retry: false,
  })

  const presetsQuery = useQuery({
    queryKey: QUERY_KEYS.presets,
    queryFn: ({ signal }) => apiGet<PresetsResponse>('/regions/presets', {}, signal),
    staleTime: 24 * 60 * 60_000,
  })

  const create = useMutation({
    mutationFn: (input: { body: RegionCreateBody; name: string }) =>
      apiPost<RegionCreateResponse>('/regions', input.body),
    onSuccess: (response, input) => onCreated(response.region_id, input.name, input.body),
  })

  const presets = presetsQuery.data?.items ?? []
  const cities = presets.filter((preset) => preset.kind === 'city')
  const states = presets.filter((preset) => preset.kind === 'state')
  const pending = create.isPending

  function submitPreset(presetId: string) {
    const preset = presets.find((item) => item.id === presetId)
    if (!preset || pending) return
    create.mutate({ body: { preset_id: preset.id }, name: preset.name })
  }

  function submitSearchResult(name: string, lat: number, lon: number) {
    if (pending) return
    create.mutate({
      body: { kind: 'city', name, point: { lat, lon } },
      name,
    })
  }

  function submitBbox(event: FormEvent) {
    event.preventDefault()
    if (pending) return
    const bbox: Bbox = {
      min_lat: Number(bboxDraft.minLat),
      min_lon: Number(bboxDraft.minLon),
      max_lat: Number(bboxDraft.maxLat),
      max_lon: Number(bboxDraft.maxLon),
    }
    const errors = validateBbox(bbox)
    if (errors.length > 0) {
      setBboxErrors(errors)
      return
    }
    setBboxErrors([])
    const name = bboxDraft.name.trim()
    create.mutate({
      body: {
        kind: 'bbox',
        name,
        bbox: {
          min_lat: bbox.min_lat,
          min_lon: bbox.min_lon,
          max_lat: bbox.max_lat,
          max_lon: bbox.max_lon,
        },
      },
      name: name || 'custom box',
    })
    setBboxDraft(EMPTY_BBOX)
  }

  const createError = create.error

  return (
    <section className="controls__group" aria-label="Region selection">
      <div className="field">
        <label htmlFor="region-search">Search a place</label>
        <input
          id="region-search"
          type="search"
          value={query}
          disabled={disabled || pending}
          placeholder="e.g. Hyderabad"
          autoComplete="off"
          aria-describedby="region-search-hint"
          onChange={(event) => setQuery(event.target.value)}
        />
        <p className="hint" id="region-search-hint">
          Debounced 300 ms; rate-limited to 10 searches/min.
        </p>
        {searchEnabled && searchQuery.isFetching && (
          <p className="hint" data-testid="search-busy">
            Searching…
          </p>
        )}
        {searchQuery.isError && (
          <p className="field-error" role="alert" data-testid="search-error">
            {searchErrorMessage(searchQuery.error)}
          </p>
        )}
        {searchQuery.isSuccess && searchQuery.data.items.length === 0 && (
          <p className="hint">No matches — try the coordinates form below.</p>
        )}
        {searchQuery.isSuccess && searchQuery.data.items.length > 0 && (
          <ul className="results" data-testid="search-results">
            {searchQuery.data.items.map((item) => (
              <li key={`${item.name}:${item.lat}:${item.lon}`}>
                <button
                  type="button"
                  className="results__item"
                  disabled={pending}
                  onClick={() => submitSearchResult(item.name, item.lat, item.lon)}
                >
                  <span>{item.name}</span>
                  <span className="hint">
                    {[item.admin1, item.country].filter(Boolean).join(', ')}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="field">
        <label htmlFor="preset-select">Curated presets</label>
        <select
          id="preset-select"
          value=""
          disabled={disabled || pending || presetsQuery.isPending || presetsQuery.isError}
          onChange={(event) => submitPreset(event.target.value)}
        >
          <option value="" disabled>
            {presetsQuery.isError ? 'Browse unavailable' : 'Choose a city or state…'}
          </option>
          <optgroup label="Cities">
            {cities.map((preset) => (
              <option key={preset.id} value={preset.id}>
                {preset.name}
              </option>
            ))}
          </optgroup>
          <optgroup label="States">
            {states.map((preset) => (
              <option key={preset.id} value={preset.id}>
                {preset.name}
              </option>
            ))}
          </optgroup>
        </select>
        {presetsQuery.isError && (
          <p className="field-error" role="alert" data-testid="presets-error">
            Browse unavailable — enter coordinates manually.
          </p>
        )}
      </div>

      <form className="field bbox-form" onSubmit={submitBbox} noValidate>
        <fieldset disabled={disabled || pending}>
          <legend>Custom coordinates (bounding box)</legend>
          <div className="bbox-grid">
            <label htmlFor="bbox-min-lat">Min lat</label>
            <input
              id="bbox-min-lat"
              type="number"
              step="any"
              value={bboxDraft.minLat}
              onChange={(event) => setBboxDraft({ ...bboxDraft, minLat: event.target.value })}
            />
            <label htmlFor="bbox-max-lat">Max lat</label>
            <input
              id="bbox-max-lat"
              type="number"
              step="any"
              value={bboxDraft.maxLat}
              onChange={(event) => setBboxDraft({ ...bboxDraft, maxLat: event.target.value })}
            />
            <label htmlFor="bbox-min-lon">Min lon</label>
            <input
              id="bbox-min-lon"
              type="number"
              step="any"
              value={bboxDraft.minLon}
              onChange={(event) => setBboxDraft({ ...bboxDraft, minLon: event.target.value })}
            />
            <label htmlFor="bbox-max-lon">Max lon</label>
            <input
              id="bbox-max-lon"
              type="number"
              step="any"
              value={bboxDraft.maxLon}
              onChange={(event) => setBboxDraft({ ...bboxDraft, maxLon: event.target.value })}
            />
            <label htmlFor="bbox-name">Label (optional)</label>
            <input
              id="bbox-name"
              type="text"
              maxLength={80}
              value={bboxDraft.name}
              placeholder="My box"
              onChange={(event) => setBboxDraft({ ...bboxDraft, name: event.target.value })}
            />
          </div>
          {bboxErrors.length > 0 && (
            <p className="field-error" role="alert" data-testid="bbox-error">
              {bboxErrors[0].reason}
            </p>
          )}
          <button type="submit" className="btn btn--primary" disabled={pending}>
            {pending ? 'Adding…' : 'Analyze this box'}
          </button>
        </fieldset>
      </form>

      {createError && (
        <p className="field-error" role="alert" data-testid="create-error">
          {createErrorMessage(createError)}
          {createError instanceof ApiError && createError.details?.field
            ? ` (${createError.details.field})`
            : ''}
        </p>
      )}
    </section>
  )
}
