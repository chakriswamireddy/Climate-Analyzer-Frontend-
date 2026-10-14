import {
  BarElement,
  CategoryScale,
  Chart as ChartJS,
  Legend,
  LineElement,
  LinearScale,
  PointElement,
  Tooltip,
} from 'chart.js'
import { Bar } from 'react-chartjs-2'
import type { ChartData, ChartOptions } from 'chart.js'
import type { OlsStats, SeriesRow, TimeseriesResponse } from '../lib/types'

ChartJS.register(CategoryScale, LinearScale, BarElement, LineElement, PointElement, Tooltip, Legend)

/** NFR-04 / FR-05: frequency + intensity bars, 5-year trailing rolling mean
 * (server-computed) and the server-computed OLS line. Low-coverage years are
 * tinted, missing years render as gaps — never fabricated zeros (CF-12/EC-25). */

export type ChartMetric = 'heatwave_days' | 'event_count' | 'mean_exceedance'

const METRICS: { id: ChartMetric; button: string; axis: string }[] = [
  { id: 'heatwave_days', button: 'Days', axis: 'Heatwave days per year' },
  { id: 'event_count', button: 'Events', axis: 'Events per year' },
  { id: 'mean_exceedance', button: 'Intensity (°C)', axis: 'Mean exceedance above threshold (°C)' },
]

const COLOR_BAR = '#0072B2'
const COLOR_LOW = '#E69F00'
const COLOR_ROLLING = '#D55E00'
const COLOR_OLS = '#3d3d3d'

export interface TimeSeriesChartProps {
  series: SeriesRow[]
  rolling: TimeseriesResponse['rolling_mean']
  ols: OlsStats | null
  metric: ChartMetric
  onMetricChange: (metric: ChartMetric) => void
  hazardLabel: string
  regionLabel: string
  startYear: number
  endYear: number
}

export function TimeSeriesChart({
  series,
  rolling,
  ols,
  metric,
  onMetricChange,
  hazardLabel,
  regionLabel,
  startYear,
  endYear,
}: TimeSeriesChartProps) {
  const axis = METRICS.find((entry) => entry.id === metric)?.axis ?? metric
  const labels = series.map((row) => String(row.year))
  const values = series.map((row) => (row.coverage_pct > 0 ? row[metric] : null))
  const colors = series.map((row) =>
    row.coverage_pct > 0 && row.coverage_pct < 80 ? COLOR_LOW : COLOR_BAR,
  )

  const rollingByYear = new Map(rolling.values.map((entry) => [entry.year, entry.value]))
  const showRolling = metric === 'heatwave_days'
  const showOls =
    metric === 'heatwave_days' && series.length >= 2 && ols !== null && ols.intercept !== null

  const datasets = [
    {
      type: 'bar' as const,
      label: axis,
      data: values,
      backgroundColor: colors,
      borderColor: colors,
      borderWidth: 0,
      spanGaps: false,
    },
    ...(showRolling
      ? [
          {
            type: 'line' as const,
            label: `${rolling.window}-year rolling mean`,
            data: series.map((row) => rollingByYear.get(row.year) ?? null),
            borderColor: COLOR_ROLLING,
            backgroundColor: COLOR_ROLLING,
            borderWidth: 2,
            pointRadius: 0,
            tension: 0.25,
            spanGaps: false,
          },
        ]
      : []),
    ...(showOls && ols
      ? [
          {
            type: 'line' as const,
            label: 'OLS trend',
            data: series.map((row) => ols.intercept! + ols.slope_per_year * row.year),
            borderColor: COLOR_OLS,
            backgroundColor: COLOR_OLS,
            borderDash: [6, 4],
            borderWidth: 1.5,
            pointRadius: 0,
            spanGaps: false,
          },
        ]
      : []),
  ]

  const options: ChartOptions<'bar'> = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { position: 'bottom' },
      tooltip: { callbacks: { label: (ctx) => `${ctx.dataset.label}: ${ctx.formattedValue}` } },
    },
    scales: {
      y: {
        beginAtZero: true,
        title: { display: true, text: axis },
        ticks: { precision: metric === 'mean_exceedance' ? 1 : 0 },
      },
      x: { title: { display: true, text: 'Year' } },
    },
  }

  const slope = ols?.slope_per_decade
  const summaryText = [
    `${axis} for ${regionLabel || 'the selected region'}, ${startYear}–${endYear}.`,
    showRolling ? `${rolling.window}-year trailing rolling mean overlaid.` : null,
    showOls && slope !== undefined && ols?.p_value != null
      ? `OLS trend ${slope > 0 ? '+' : ''}${slope} days/decade (p=${ols.p_value}).`
      : null,
    series.length < 2 ? 'Trend needs at least 2 years of data.' : null,
    'Years below 80% coverage are shaded orange; missing years are gaps, not zeros.',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <figure className="card chart-card">
      <div className="chart-card__head">
        <h2>{hazardLabel} over time</h2>
        <div className="metric-toggle" role="group" aria-label="Chart metric">
          {METRICS.map((entry) => (
            <button
              key={entry.id}
              type="button"
              className="btn btn--small"
              aria-pressed={metric === entry.id}
              onClick={() => onMetricChange(entry.id)}
            >
              {entry.button}
            </button>
          ))}
        </div>
      </div>
      <div className="chart-canvas" role="img" aria-label={summaryText}>
        <Bar data={{ labels, datasets } as ChartData<'bar', (number | null)[]>} options={options} />
      </div>
      <figcaption className="a11y-text" data-testid="chart-summary">
        {summaryText}
      </figcaption>
    </figure>
  )
}
