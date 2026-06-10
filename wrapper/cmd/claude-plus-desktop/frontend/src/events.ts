// Hand-typed mirrors of internal/event's Envelope/Event JSON (camelCase) plus
// the event-kind constants. Wails generates models only for bound-method types,
// not event payloads, so these are maintained by hand — keep in sync with the
// Go event package.

export interface Ev {
  kind: string;
  sessionId: string;
  name?: string;
  tool?: string;
  summary?: string;
}

export interface Envelope {
  ts: number;
  seq: number;
  event: Ev;
}

// Event kinds carried on Envelope.event.kind.
export const KIND_SESSION_START = 'session.start';
export const KIND_SESSION_RENAME = 'session.rename';
export const KIND_TOOL_CALL = 'tool.call';
export const KIND_TOOL_RESULT = 'tool.result';
export const KIND_USER_MSG = 'user.msg';
export const KIND_ASSISTANT_MSG = 'assistant.msg';
export const KIND_STATUS_CHANGE = 'status.change';
