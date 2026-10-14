import { useEffect, useState } from 'react'
import { validateRange, type FieldError } from '../lib/validation'
import { DEFAULT_END_YEAR, DEFAULT_START_YEAR, lastFullYear } from '../lib/urlParams'

/** FR-04 range picker with the V-01/V-02 mirror: an invalid edit never
 * reaches the URL or the API — it is blocked inline (TC-F-02). */

export interface RangePickerProps {
  startYear: number
  endYear: number
  onChange: (range: { startYear: number; endYear: number }) => void
  disabled?: boolean
}

function draftError(raw: string, field: string): FieldError | null {
  if (raw.trim() === '') return { field, reason: `${field} is required` }
  if (!/^-?\d+$/.test(raw.trim())) return { field, reason: `${field} must be a whole number` }
  return null
}

export function RangePicker({ startYear, endYear, onChange, disabled }: RangePickerProps) {
  const [startDraft, setStartDraft] = useState(String(startYear))
  const [endDraft, setEndDraft] = useState(String(endYear))
  const [errors, setErrors] = useState<FieldError[]>(() => validateRange(startYear, endYear))

  useEffect(() => {
    setStartDraft(String(startYear))
    setEndDraft(String(endYear))
  }, [startYear, endYear])

  function apply(nextStart: string, nextEnd: string) {
    const structural = draftError(nextStart, 'start_year') ?? draftError(nextEnd, 'end_year')
    if (structural) {
      setErrors([structural])
      return
    }
    const range = { startYear: Number(nextStart), endYear: Number(nextEnd) }
    const rangeErrors = validateRange(range.startYear, range.endYear)
    if (rangeErrors.length > 0) {
      setErrors(rangeErrors)
      return
    }
    setErrors([])
    if (range.startYear !== startYear || range.endYear !== endYear) onChange(range)
  }

  function commitPreset(start: number, end: number) {
    setErrors([])
    onChange({ startYear: start, endYear: end })
  }

  const message = errors[0]

  return (
    <div className="field field--range">
      <span className="field__label" id="range-label">
        Time range
      </span>
      <div className="range-inputs">
        <div className="field field--compact">
          <label htmlFor="start-year">Start year</label>
          <input
            id="start-year"
            type="number"
            inputMode="numeric"
            min={1950}
            max={lastFullYear()}
            value={startDraft}
            disabled={disabled}
            aria-invalid={Boolean(message && message.field === 'start_year')}
            onChange={(event) => {
              setStartDraft(event.target.value)
              apply(event.target.value, endDraft)
            }}
          />
        </div>
        <div className="field field--compact">
          <label htmlFor="end-year">End year</label>
          <input
            id="end-year"
            type="number"
            inputMode="numeric"
            min={1950}
            max={lastFullYear()}
            value={endDraft}
            disabled={disabled}
            aria-invalid={Boolean(message && message.field === 'end_year')}
            onChange={(event) => {
              setEndDraft(event.target.value)
              apply(startDraft, event.target.value)
            }}
          />
        </div>
      </div>
      {message && (
        <p className="field-error" role="alert" data-testid="range-error">
          {message.reason}
        </p>
      )}
      <div className="range-presets">
        <button
          type="button"
          className="btn btn--small"
          disabled={disabled}
          onClick={() => commitPreset(DEFAULT_START_YEAR, DEFAULT_END_YEAR)}
        >
          1990–2020
        </button>
        <button
          type="button"
          className="btn btn--small"
          disabled={disabled}
          onClick={() => commitPreset(1950, lastFullYear())}
        >
          Full record
        </button>
      </div>
    </div>
  )
}
