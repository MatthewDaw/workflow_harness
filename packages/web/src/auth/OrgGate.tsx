import { useState, type FormEvent, type ReactNode } from 'react';
import { Wordmark } from '../components/Emblem.js';
import { useGetMeQuery, useCreateOrgMutation, useJoinOrgMutation } from '../api/baseApi.js';

/**
 * Org-onboarding gate (org-onboarding): mounts inside LoginGate, so it only runs
 * for an authenticated user. It reads the caller's REAL membership via `GET /me`
 * — which never falls back to the token claim — and, when they have no org,
 * forces them to either create a new org or join an existing one before the app
 * (router) mounts. Membership is the source of truth for scoping every dataset,
 * so the app must not render until the user is in an org.
 */

type Mode = 'create' | 'join';

/** RTK Query surfaces fetchBaseQuery errors as `{ status, data }`; dig out the
 * server's `{ error }` message (falling back to a sensible default) so 400/409/
 * 403 bodies render in the alert verbatim. */
function errMessage(err: unknown, fallback: string): string {
  const data = (err as { data?: unknown } | undefined)?.data;
  if (data && typeof data === 'object' && 'error' in data) {
    const msg = (data as { error?: unknown }).error;
    if (typeof msg === 'string' && msg) return msg;
  }
  return fallback;
}

export function OrgGate({ children }: { children: ReactNode }) {
  const { data, isLoading } = useGetMeQuery();

  if (isLoading) {
    return (
      <div className="grid min-h-screen place-items-center text-mut" role="status">
        Loading…
      </div>
    );
  }

  // Real membership present → the user is onboarded; hand off to the app.
  if (data?.org) return <>{children}</>;

  return <Onboarding />;
}

/**
 * The create/join card. Kept as its own component so its form state only exists
 * while onboarding is actually shown (and resets cleanly once the gate flips).
 */
function Onboarding() {
  const [mode, setMode] = useState<Mode>('create');
  const [name, setName] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);

  const [createOrg, createState] = useCreateOrgMutation();
  const [joinOrg, joinState] = useJoinOrgMutation();
  const submitting = createState.isLoading || joinState.isLoading;

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      if (mode === 'create') {
        // On success the 'Me' invalidation refetches /me; the gate sees the new
        // org and flips to the app automatically — nothing to do here.
        await createOrg({ name, password }).unwrap();
      } else {
        await joinOrg({ name, password }).unwrap();
      }
    } catch (err) {
      // Join always shows ONE fixed message — we deliberately ignore the server
      // body so the UI never hints whether the name or the password was wrong
      // (the backend already sends a generic 403 to avoid org enumeration).
      // Create surfaces the server's validation/409 text so the user can fix it.
      setError(
        mode === 'join'
          ? 'Invalid organization name or password.'
          : errMessage(err, 'Could not create organization.'),
      );
    }
  }

  // Toggling tabs clears the inline error so a stale create/join message never
  // lingers under the other tab.
  function selectMode(next: Mode) {
    setMode(next);
    setError(null);
  }

  return (
    <div className="grid min-h-screen place-items-center bg-bg">
      <form
        onSubmit={onSubmit}
        aria-label="Set up your organization"
        className="hq-frame w-[340px] p-6"
      >
        <div className="mb-2">
          <Wordmark size={34} />
        </div>
        <div className="mb-5 text-xs text-mut">
          Join your team's organization or create a new one to get started.
        </div>

        {/* Two tabs toggle the form between creating and joining an org. */}
        <div
          className="mb-4 grid grid-cols-2 gap-1 rounded-md border border-line p-1"
          role="tablist"
          aria-label="Organization mode"
        >
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'create'}
            onClick={() => selectMode('create')}
            className={`hq-btn ${mode === 'create' ? 'hq-btn-pri' : ''}`}
          >
            Create organization
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'join'}
            onClick={() => selectMode('join')}
            className={`hq-btn ${mode === 'join' ? 'hq-btn-pri' : ''}`}
          >
            Join organization
          </button>
        </div>

        <label
          className="mb-1 block text-[11px] uppercase tracking-wide text-faint"
          htmlFor="org-name"
        >
          Organization name
        </label>
        <input
          id="org-name"
          name="org-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="mb-3 w-full rounded-md border border-line px-3 py-2 text-sm"
        />

        <label
          className="mb-1 block text-[11px] uppercase tracking-wide text-faint"
          htmlFor="org-password"
        >
          Password
        </label>
        <input
          id="org-password"
          name="org-password"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="mb-3 w-full rounded-md border border-line px-3 py-2 text-sm"
        />

        {mode === 'join' && (
          <div className="mb-3 text-[11px] text-faint">
            Type the organization's exact name and password.
          </div>
        )}

        {error && (
          <div role="alert" className="mb-3 text-xs text-live">
            {error}
          </div>
        )}

        <button type="submit" disabled={submitting} className="hq-btn hq-btn-pri w-full">
          {mode === 'create'
            ? submitting
              ? 'Creating…'
              : 'Create organization'
            : submitting
              ? 'Joining…'
              : 'Join organization'}
        </button>
      </form>
    </div>
  );
}
