import { useState, type FormEvent } from 'react';
import { useCreateOrgMutation, useJoinOrgMutation } from '../api/baseApi.js';

/**
 * The create / join organization card — the form half of onboarding, factored
 * out of OrgGate so it can be reused in-app (the "Join or create organization"
 * screen reachable from the header switcher) as well as at the login gate.
 *
 * On a successful create/join the org mutations invalidate `Me` (and reset every
 * org-scoped dataset), so any mounted OrgGate flips automatically; `onSuccess`
 * lets an in-app caller also react (e.g. navigate back into the app).
 */

type Mode = 'create' | 'join';

/** RTK Query surfaces fetchBaseQuery errors as `{ status, data }`; dig out the
 * server's `{ error }` message so 400/409 bodies render verbatim in the alert. */
function errMessage(err: unknown, fallback: string): string {
  const data = (err as { data?: unknown } | undefined)?.data;
  if (data && typeof data === 'object' && 'error' in data) {
    const msg = (data as { error?: unknown }).error;
    if (typeof msg === 'string' && msg) return msg;
  }
  return fallback;
}

export function OrgForms({ onSuccess }: { onSuccess?: () => void }) {
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
        await createOrg({ name, password }).unwrap();
      } else {
        await joinOrg({ name, password }).unwrap();
      }
      setName('');
      setPassword('');
      onSuccess?.();
    } catch (err) {
      // Join shows ONE fixed message (the server sends a generic 403 to avoid org
      // enumeration); create surfaces the server's validation/409 text.
      setError(
        mode === 'join'
          ? 'Invalid organization name or password.'
          : errMessage(err, 'Could not create organization.'),
      );
    }
  }

  // Toggling tabs clears the inline error so a stale message never lingers.
  function selectMode(next: Mode) {
    setMode(next);
    setError(null);
  }

  return (
    <form onSubmit={onSubmit} aria-label="Set up your organization" className="hq-frame w-[340px] p-6">
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
  );
}
