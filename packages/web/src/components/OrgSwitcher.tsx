import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useGetMeQuery, useSwitchOrgMutation } from '../api/baseApi.js';

/**
 * Header org control: shows the ACTIVE org and opens a menu to switch to another
 * org the user has joined, or to go create/join a new one. Switching swaps the
 * whole tenant — the mutation resets the API cache — so we route to Objectives
 * afterwards rather than leave the user on a now-foreign deep link.
 */
export function OrgSwitcher() {
  const { data: me } = useGetMeQuery();
  const [switchOrg, { isLoading }] = useSwitchOrgMutation();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);

  const active = me?.org ?? null;
  if (!active) return null; // OrgGate handles the no-org case
  const others = (me?.orgs ?? []).filter((o) => o !== active);

  async function choose(org: string) {
    setOpen(false);
    try {
      await switchOrg({ org }).unwrap();
      navigate('/objectives');
    } catch {
      // Membership check failed server-side; stay put (the menu already closed).
    }
  }

  return (
    <span className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        disabled={isLoading}
        className="flex items-center gap-1 text-cream/90"
        title="Organization"
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <span className="font-semibold">{active}</span>
        <span aria-hidden className="text-cream/50 text-[10px]">
          ▼
        </span>
      </button>

      {open && (
        <>
          {/* Invisible backdrop closes the menu on any outside click. */}
          <button
            type="button"
            aria-hidden
            tabIndex={-1}
            className="fixed inset-0 z-10 cursor-default"
            onClick={() => setOpen(false)}
          />
          <div
            role="menu"
            aria-label="Switch organization"
            className="absolute right-0 z-20 mt-2 w-56 rounded-md border border-odd bg-odd p-1 text-left shadow-frame"
          >
            {others.length > 0 && (
              <>
                <div className="px-2 py-1 text-[10px] uppercase tracking-wide text-cream/50">
                  Switch organization
                </div>
                {others.map((o) => (
                  <button
                    key={o}
                    type="button"
                    role="menuitem"
                    onClick={() => void choose(o)}
                    className="block w-full truncate rounded px-2 py-1.5 text-left text-xs text-cream/85 hover:bg-od"
                  >
                    {o}
                  </button>
                ))}
                <div className="my-1 h-px bg-od" />
              </>
            )}
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                navigate('/organizations');
              }}
              className="block w-full rounded px-2 py-1.5 text-left text-xs text-cream/85 hover:bg-od"
            >
              Join or create organization…
            </button>
          </div>
        </>
      )}
    </span>
  );
}
