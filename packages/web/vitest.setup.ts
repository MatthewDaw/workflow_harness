import '@testing-library/jest-dom/vitest';

// jsdom and Node's undici `fetch` disagree on AbortSignal identity: RTK Query
// attaches an AbortController to every request, and undici rejects the
// jsdom-realm signal with "Expected signal to be an instance of AbortSignal".
// Any component that auto-fires a query on mount then surfaces this as an
// UNHANDLED rejection, which fails the whole run even though every test passes.
//
// Install a default `fetch` baseline that cleanly rejects: fetchBaseQuery catches
// baseQuery rejections and lands the query in a normal FETCH_ERROR state (the same
// outcome the undici throw produced), so there is no unhandled rejection. Tests
// that need real fetch behaviour stub it via `vi.stubGlobal('fetch', …)` and
// `vi.unstubAllGlobals()` restores this baseline.
globalThis.fetch = (() =>
  Promise.reject(new Error('fetch is not mocked in this test'))) as typeof fetch;
