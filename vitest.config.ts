import { defineConfig } from 'vitest/config'

// Scoped to the desktop frontend's pure-logic unit tests (src/**). Backend
// Python code has its own pytest suite, and Backend/bridges/* Node code uses
// the built-in node:test runner - both are intentionally excluded so `npm
// test` stays fast and doesn't try (and fail) to load runners it can't use.
export default defineConfig({
  test: {
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
