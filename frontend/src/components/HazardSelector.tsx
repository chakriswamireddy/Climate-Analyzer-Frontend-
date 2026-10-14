import type { HazardItem } from '../lib/types'

/** FR-03: the list comes from `/hazards` (never hard-coded) and the API
 * default is preselected. */

export interface HazardSelectorProps {
  items: HazardItem[]
  defaultId: string
  value: string
  onChange: (hazardId: string) => void
  disabled?: boolean
}

export function HazardSelector({
  items,
  defaultId,
  value,
  onChange,
  disabled,
}: HazardSelectorProps) {
  const selected = items.find((item) => item.id === value)
  const selectValue = selected ? value : defaultId
  const shown = selected ?? items.find((item) => item.id === defaultId) ?? items[0]
  return (
    <div className="field">
      <label htmlFor="hazard-select">Hazard</label>
      <select
        id="hazard-select"
        value={selectValue}
        disabled={disabled || items.length === 0}
        onChange={(event) => onChange(event.target.value)}
      >
        {items.map((item) => (
          <option key={item.id} value={item.id}>
            {item.label}
          </option>
        ))}
      </select>
      {shown && (
        <p className="hint" id="hazard-hint">
          {shown.description}
        </p>
      )}
    </div>
  )
}
