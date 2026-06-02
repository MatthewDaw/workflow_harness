import { createHmac, timingSafeEqual } from 'node:crypto';
import { isValidTicketTransition, type Ticket } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import { extractTicketIds } from './history.js';

/**
 * GitHub webhook handler (U26, KTD9).
 *
 * Verifies the `X-Hub-Signature-256` HMAC over the raw body, then moves linked
 * tickets when a PR opens (→ in_review) or merges (→ done). Tickets are linked
 * by id convention (`<TICKET-ID>` in the PR branch, title, or body). Signature
 * verification is mandatory: an unsigned or mismatched payload is rejected
 * before any state change.
 */

/**
 * Verify the GitHub `sha256=…` signature against the raw request body using the
 * shared webhook secret. Constant-time comparison; returns false on any
 * malformed input rather than throwing.
 */
export function verifySignature(
  rawBody: string,
  signatureHeader: string | undefined,
  secret: string,
): boolean {
  if (!signatureHeader || !signatureHeader.startsWith('sha256=')) return false;
  const expected = 'sha256=' + createHmac('sha256', secret).update(rawBody, 'utf8').digest('hex');
  const a = Buffer.from(signatureHeader);
  const b = Buffer.from(expected);
  if (a.length !== b.length) return false;
  return timingSafeEqual(a, b);
}

/** The subset of a `pull_request` webhook payload we act on. */
export interface PullRequestPayload {
  action: string; // opened | reopened | closed | …
  repository?: { full_name?: string };
  pull_request?: {
    number?: number;
    title?: string;
    body?: string;
    merged?: boolean;
    html_url?: string;
    head?: { ref?: string };
  };
}

export interface WebhookDeps {
  repo: Repo;
  /** The shared webhook secret (HMAC key). */
  secret: string;
}

export interface WebhookResult {
  statusCode: number;
  body: string;
}

/**
 * Decide the target ticket status for a PR event, or undefined to ignore:
 *  - opened / reopened  -> in_review
 *  - closed & merged    -> done
 *  - anything else      -> (ignored)
 */
export function targetStatusFor(payload: PullRequestPayload): 'in_review' | 'done' | undefined {
  if (payload.action === 'opened' || payload.action === 'reopened') return 'in_review';
  if (payload.action === 'closed' && payload.pull_request?.merged === true) return 'done';
  return undefined;
}

/**
 * Handle a verified `pull_request` event: resolve the project from the repo
 * full-name, find tickets referenced by the PR (branch/title/body), and apply
 * the lifecycle transition. Invalid transitions for a given ticket are skipped
 * (not fatal); the PR may legitimately touch a ticket already past that state.
 */
export async function handlePullRequest(
  payload: PullRequestPayload,
  deps: WebhookDeps,
): Promise<{ moved: { ticketId: string; to: string }[] }> {
  const to = targetStatusFor(payload);
  if (!to) return { moved: [] };

  const repoFullName = payload.repository?.full_name;
  if (!repoFullName) return { moved: [] };
  const projectId = await deps.repo.getProjectIdForRepo(repoFullName);
  if (!projectId) return { moved: [] };

  const pr = payload.pull_request ?? {};
  const ticketIds = extractTicketIds(pr.head?.ref, pr.title, pr.body);

  const moved: { ticketId: string; to: string }[] = [];
  for (const ticketId of ticketIds) {
    const ticket = await deps.repo.getTicket(projectId, ticketId);
    if (!ticket) continue;
    if (!isValidTicketTransition(ticket.status, to)) continue;

    const updated: Ticket = {
      ...ticket,
      status: to,
      pr: pr.html_url ?? pr.number?.toString() ?? ticket.pr,
      branch: pr.head?.ref ?? ticket.branch,
    };
    await deps.repo.putTicket(updated);
    moved.push({ ticketId, to });
  }
  return { moved };
}

/**
 * Entry point: verify the signature then dispatch by event type. Returns a 401
 * for a bad signature, 200 for accepted (including ignored) events. The raw body
 * MUST be the exact bytes the signature was computed over.
 */
export async function handleWebhook(
  opts: {
    rawBody: string;
    eventType: string | undefined;
    signature: string | undefined;
  },
  deps: WebhookDeps,
): Promise<WebhookResult> {
  if (!verifySignature(opts.rawBody, opts.signature, deps.secret)) {
    return { statusCode: 401, body: JSON.stringify({ error: 'invalid signature' }) };
  }

  let payload: unknown;
  try {
    payload = JSON.parse(opts.rawBody);
  } catch {
    return { statusCode: 400, body: JSON.stringify({ error: 'invalid JSON' }) };
  }

  if (opts.eventType === 'pull_request') {
    const result = await handlePullRequest(payload as PullRequestPayload, deps);
    return { statusCode: 200, body: JSON.stringify(result) };
  }

  // Other events (push, ping, …) are acknowledged but not acted on here.
  return { statusCode: 200, body: JSON.stringify({ ignored: opts.eventType ?? 'unknown' }) };
}
