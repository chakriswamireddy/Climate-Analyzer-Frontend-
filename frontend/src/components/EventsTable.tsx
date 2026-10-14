import type { EventItem } from '../lib/types'

/** Paginated event list (docs/02 §7.4, 03 §7) — offset paging with an
 * always-visible total so the user knows the dataset size (TC-F-09). */

export interface EventsTableProps {
  items: EventItem[]
  page: number
  pageSize: number
  total: number
  pending?: boolean
  hazardLabel: string
  startYear: number
  endYear: number
  onPageChange: (page: number) => void
}

export function EventsTable({
  items,
  page,
  pageSize,
  total,
  pending,
  hazardLabel,
  startYear,
  endYear,
  onPageChange,
}: EventsTableProps) {
  const pages = Math.max(1, Math.ceil(total / pageSize))
  return (
    <figure className="card">
      <figcaption>
        <h2 className="inline-title">
          {hazardLabel} events, {startYear}–{endYear}
        </h2>
      </figcaption>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">Start</th>
              <th scope="col">End</th>
              <th scope="col">Days</th>
              <th scope="col">Mean exceedance (°C)</th>
              <th scope="col">Peak exceedance (°C)</th>
            </tr>
          </thead>
          <tbody aria-busy={Boolean(pending)}>
            {items.length === 0 ? (
              <tr>
                <td colSpan={5}>{pending ? 'Loading events…' : 'No events on this page.'}</td>
              </tr>
            ) : (
              items.map((event) => (
                <tr key={event.event_id}>
                  <td>{event.start_date}</td>
                  <td>{event.end_date}</td>
                  <td>{event.duration_days}</td>
                  <td>{event.mean_exceedance.toFixed(1)}</td>
                  <td>{event.peak_exceedance.toFixed(1)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      <nav className="pagination" aria-label="Events pagination">
        <button
          type="button"
          className="btn btn--small"
          disabled={page <= 1 || pending}
          onClick={() => onPageChange(page - 1)}
        >
          Previous
        </button>
        <span data-testid="events-pagination" aria-live="polite">
          Page {page} of {pages} · {total} events
        </span>
        <button
          type="button"
          className="btn btn--small"
          disabled={page >= pages || pending}
          onClick={() => onPageChange(page + 1)}
        >
          Next
        </button>
      </nav>
    </figure>
  )
}
