import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach, expect, vi } from 'vitest'

/** M-05: the app must render without console errors — any React warning or
 * unhandled rejection that reaches console.error fails the test that caused
 * it, with the original message included. */
let consoleErrors: unknown[][] = []

beforeEach(() => {
  consoleErrors = []
  vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
    consoleErrors.push(args)
  })
})

afterEach(() => {
  cleanup()
  expect(consoleErrors, 'console.error was called during the test').toEqual([])
})
