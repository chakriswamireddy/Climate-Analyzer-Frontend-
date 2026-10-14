/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Absolute origin of the deployed API (e.g. https://climate-api.onrender.com).
   * Empty/undefined in dev and tests, where the Vite proxy serves /api locally. */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
