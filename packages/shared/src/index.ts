// @harness/shared — single source of truth for the event schema, API DTOs,
// and scope-resolution logic shared across backend, web, and (via the golden
// fixture in test/golden) the Go wrapper.

export * from './events.js';
export * from './scope.js';
export * from './dto.js';

export const SHARED_PACKAGE_VERSION = '0.1.0';
