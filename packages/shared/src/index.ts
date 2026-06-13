// @harness/shared — single source of truth for the event schema, API DTOs,
// scope-resolution logic, and learning IDL schema types shared across backend,
// web, and (via the golden fixture in test/golden) the Go wrapper.

export * from './events.js';
export * from './scope.js';
export * from './dto.js';
// IDL-generated learning schema (revisionKey, truePointerKey, record shapes).
// The canonical IDL lives in packages/learning-service/src/learning_service/schema/idl.py
// and is code-generated into both Python and TypeScript.  This re-export makes
// the generated shapes available to all TS consumers via @harness/shared so
// the hand-authored mirrors in db/keys.ts are no longer needed.
export * from './learning-schema.js';
