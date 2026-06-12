/**
 * python-writer-client — thin TS client for the Python author-revision API.
 *
 * U10 / MAT-141 Gap 1: the TS catalog-authoring UIs (hq-add-skill, web editor,
 * seeding, foldIdea) must call the ONE Python revision writer instead of writing
 * skill revisions themselves.  This module is the TS side of that contract.
 *
 * When ``PYTHON_AUTHOR_REVISION_URL`` is set in the environment, callers use
 * ``callPythonAuthorRevision`` to POST the revision request to the Python Lambda
 * endpoint.  When the env var is absent the caller falls back to the existing
 * TS DynamoDB write path (kept for local dev / tests; never used in the deployed
 * Lambda, which always has the env var set).
 *
 * This is the only TS module that may call the Python author-revision endpoint;
 * nothing else should write skill revisions directly.
 */

/** The URL of the Python author-revision Lambda endpoint. */
export function pythonAuthorRevisionUrl(): string | undefined {
  return process.env.PYTHON_AUTHOR_REVISION_URL?.trim() || undefined;
}

/** Request shape mirroring the Python handler's JSON body contract. */
export interface AuthorRevisionRequest {
  org: string;
  baseName: string;
  variantId: string;
  body: string;
  authorUserId?: string;
  description?: string;
  ideaId?: string;
  goldenCase?: {
    caseId: string;
    before: string;
    after: string;
    ideaBody: string;
  };
}

/** Response shape returned by the Python handler. */
export interface AuthorRevisionResponse {
  org: string;
  baseName: string;
  variantId: string;
  rev: number;
  truePointerUpdated: boolean;
  goldenCaseWritten: boolean;
}

/**
 * Call the Python author-revision API.
 *
 * Throws on non-2xx status (the caller decides whether to fall back or surface
 * the error).  Use this wherever the TS path previously wrote a skill revision
 * directly to DynamoDB.
 */
export async function callPythonAuthorRevision(
  req: AuthorRevisionRequest,
  fetchImpl?: (url: string, init: RequestInit) => Promise<Response>,
): Promise<AuthorRevisionResponse> {
  const baseUrl = pythonAuthorRevisionUrl();
  if (!baseUrl) {
    throw new Error(
      'callPythonAuthorRevision: PYTHON_AUTHOR_REVISION_URL is not set. ' +
        'Set the env var to point at the Python author-revision Lambda endpoint.',
    );
  }
  const fetchFn = fetchImpl ?? fetch;
  const endpoint = `${baseUrl.replace(/\/$/, '')}/internal/skills/${encodeURIComponent(req.baseName)}/revisions`;
  const resp = await fetchFn(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  });
  if (!resp.ok) {
    const body = await resp.text().catch(() => '<unreadable body>');
    throw new Error(
      `callPythonAuthorRevision: Python writer returned ${resp.status}: ${body}`,
    );
  }
  return (await resp.json()) as AuthorRevisionResponse;
}

/**
 * Returns true when the TS authoring path should route through the Python writer
 * (i.e. when ``PYTHON_AUTHOR_REVISION_URL`` is configured).
 *
 * Use this as a feature flag at the call site:
 *
 *   if (usesPythonWriter()) {
 *     return callPythonAuthorRevision(req);
 *   }
 *   // legacy TS DynamoDB path
 */
export function usesPythonWriter(): boolean {
  return !!pythonAuthorRevisionUrl();
}
