import { QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import App from '../App'
import { makeQueryClient, type QueryClientOptions } from '../lib/queries'

/** Mounts the full app against a fresh QueryClient and a given URL, so every
 * test starts from a shareable-link state (docs/02 §7.4). */
export function renderApp(url = '/', options: QueryClientOptions = {}) {
  window.history.replaceState(null, '', url)
  const client = makeQueryClient(options)
  const utils = render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  )
  return { ...utils, client }
}
