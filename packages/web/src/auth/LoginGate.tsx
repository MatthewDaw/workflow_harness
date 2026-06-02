import { useState, type FormEvent, type ReactNode } from 'react';
import { useAuth } from './AuthProvider.js';

/**
 * Auth gate (U19): renders the login form when unauthenticated and the app
 * shell once a user is present. The first authenticated route is Objectives.
 */
export function LoginGate({ children }: { children: ReactNode }) {
  const { user, loading, signIn, signInWithGoogle } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  if (loading) {
    return (
      <div className="grid min-h-screen place-items-center text-mut" role="status">
        Loading…
      </div>
    );
  }

  if (user) return <>{children}</>;

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await signIn(username, password);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign in failed');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="grid min-h-screen place-items-center bg-bg">
      <form
        onSubmit={onSubmit}
        aria-label="Sign in to Command HQ"
        className="hq-frame w-[340px] p-6"
      >
        <div className="mb-1 text-lg font-bold tracking-wide">⌗ Command HQ</div>
        <div className="mb-5 text-xs text-mut">Sign in to watch &amp; steer your sessions.</div>

        <label
          className="mb-1 block text-[11px] uppercase tracking-wide text-faint"
          htmlFor="username"
        >
          Username
        </label>
        <input
          id="username"
          name="username"
          autoComplete="username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          className="mb-3 w-full rounded-md border border-line px-3 py-2 text-sm"
        />

        <label
          className="mb-1 block text-[11px] uppercase tracking-wide text-faint"
          htmlFor="password"
        >
          Password
        </label>
        <input
          id="password"
          name="password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="mb-4 w-full rounded-md border border-line px-3 py-2 text-sm"
        />

        {error && (
          <div role="alert" className="mb-3 text-xs text-live">
            {error}
          </div>
        )}

        <button type="submit" disabled={submitting} className="hq-btn hq-btn-pri w-full">
          {submitting ? 'Signing in…' : 'Sign in'}
        </button>

        <div className="my-4 flex items-center gap-3 text-[11px] uppercase tracking-wide text-faint">
          <span className="h-px flex-1 bg-line" />
          or
          <span className="h-px flex-1 bg-line" />
        </div>

        <button
          type="button"
          onClick={async () => {
            setError(null);
            try {
              await signInWithGoogle();
            } catch (err) {
              setError(err instanceof Error ? err.message : 'Google sign in failed');
            }
          }}
          className="hq-btn w-full"
        >
          Continue with Google
        </button>
      </form>
    </div>
  );
}
