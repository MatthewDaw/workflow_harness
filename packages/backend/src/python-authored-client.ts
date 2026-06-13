/**
 * python-authored-client — thin TS client for the Python authored-ingestion
 * endpoint (U8 / MAT-146).
 *
 * When ``PYTHON_AUTHORED_URL`` is set in the environment, the Memories tab
 * PUT/DELETE (``rest/memories.ts``) calls this module to bridge each memory
 * write into the Python authored lane.  This achieves two-store coherence:
 *
 *   - A MEM# row persists in DynamoDB (same key as before, via the TS path).
 *   - An authored idea is created/retired in the Python learning graph.
 *
 * When ``PYTHON_AUTHORED_URL`` is not set, the function returns `null` and
 * the caller continues with the flat MEM# write only — correct degradation
 * for local dev / tests that do not have the Python service running.
 *
 * Route: POST /internal/authored
 */

/** URL of the Python authored-ingestion Lambda (or local dev server). */
export function pythonAuthoredUrl(): string | undefined {
  return process.env.PYTHON_AUTHORED_URL?.trim() || undefined;
}

/** Whether the Python authored bridge is wired (env var present). */
export function usesPythonAuthored(): boolean {
  return !!pythonAuthoredUrl();
}

/** Request body for the Python authored endpoint. */
export interface AuthoredRequest {
  kind: 'directive' | 'paste' | 'delete' | 'remember';
  org: string;
  projectId: string;
  userId: string;
  name: string;
  content?: string;
  skillBaseName: string;
  scopeTag?: string;
}

/** Response body returned by the Python authored endpoint. */
export interface AuthoredResponse {
  sourceName: string;
  kind: string;
  nodesWritten?: number;
  nodesDeduped?: number;
  supersessions?: string[];
  unBridged?: string[];
}

/**
 * Call the Python authored-ingestion endpoint.
 *
 * Returns `null` when ``PYTHON_AUTHORED_URL`` is not configured (graceful
 * degradation — the caller still performs the flat MEM# DynamoDB write).
 *
 * Throws on network errors or non-2xx responses so the caller can surface
 * the error rather than silently losing the graph bridge.
 */
export async function callPythonAuthored(
  req: AuthoredRequest,
  fetchImpl?: (url: string, init: RequestInit) => Promise<Response>,
): Promise<AuthoredResponse | null> {
  const baseUrl = pythonAuthoredUrl();
  if (!baseUrl) return null;

  const fetchFn = fetchImpl ?? fetch;
  const endpoint = `${baseUrl.replace(/\/$/, '')}/internal/authored`;

  const resp = await fetchFn(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  });

  if (!resp.ok) {
    const body = await resp.text().catch(() => '<unreadable body>');
    throw new Error(
      `callPythonAuthored: Python authored endpoint returned ${resp.status}: ${body}`,
    );
  }

  return (await resp.json()) as AuthoredResponse;
}
