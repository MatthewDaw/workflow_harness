import { type ReactNode } from 'react';
import { Wordmark } from '../components/Emblem.js';
import { useGetMeQuery } from '../api/baseApi.js';
import { OrgForms } from './OrgForms.js';

/**
 * Org-onboarding gate: mounts inside LoginGate, so it only runs for an
 * authenticated user. It reads the caller's REAL membership via `GET /me` — which
 * never falls back to the token claim — and, when they have no ACTIVE org, forces
 * them to create or join one before the app (router) mounts. Membership is the
 * source of truth for scoping every dataset, so the app must not render until the
 * user is in an org.
 */
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

  return (
    <div className="grid min-h-screen place-items-center bg-bg">
      <div className="w-[340px]">
        <div className="mb-2">
          <Wordmark size={34} />
        </div>
        <div className="mb-5 text-xs text-mut">
          Join your team's organization or create a new one to get started.
        </div>
        <OrgForms />
      </div>
    </div>
  );
}
