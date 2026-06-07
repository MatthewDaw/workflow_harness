import { useNavigate } from 'react-router-dom';
import { useGetMeQuery, useSwitchOrgMutation } from '../../api/baseApi.js';
import { OrgForms } from '../../auth/OrgForms.js';

/**
 * In-app organization management (reached from the header switcher's "Join or
 * create organization…"). Lists every org the user belongs to so they can switch
 * the active one, and reuses the onboarding OrgForms card to create or join
 * another. Any switch/create/join swaps the whole tenant, so we route back to
 * Objectives once it lands.
 */
export function Organizations() {
  const { data: me } = useGetMeQuery();
  const [switchOrg] = useSwitchOrgMutation();
  const navigate = useNavigate();

  const active = me?.org ?? null;
  const orgs = me?.orgs ?? (active ? [active] : []);

  async function choose(org: string) {
    if (org === active) return;
    try {
      await switchOrg({ org }).unwrap();
      navigate('/objectives');
    } catch {
      // membership check failed server-side; stay on the page
    }
  }

  return (
    <div className="p-6" data-testid="organizations-screen">
      <h1 className="mb-1 font-stencil text-lg font-bold uppercase tracking-wide text-ink">
        Organizations
      </h1>
      <p className="mb-5 text-xs text-mut">
        Switch between the organizations you belong to, or join/create another. Switching
        reloads every screen with that organization's data.
      </p>

      <div className="grid gap-6 md:grid-cols-2">
        <section>
          <div className="mb-2 text-[11px] uppercase tracking-wide text-faint">Your organizations</div>
          <ul className="hq-frame divide-y divide-line2">
            {orgs.map((o) => {
              const isActive = o === active;
              return (
                <li key={o} className="flex items-center justify-between px-4 py-2.5">
                  <span className="text-sm text-ink">{o}</span>
                  {isActive ? (
                    <span className="text-[11px] uppercase tracking-wide text-good">Active</span>
                  ) : (
                    <button
                      type="button"
                      onClick={() => void choose(o)}
                      className="hq-btn text-[11px]"
                    >
                      Switch
                    </button>
                  )}
                </li>
              );
            })}
            {orgs.length === 0 && (
              <li className="px-4 py-2.5 text-xs text-mut">You are not in any organization yet.</li>
            )}
          </ul>
        </section>

        <section>
          <div className="mb-2 text-[11px] uppercase tracking-wide text-faint">
            Join or create another
          </div>
          {/* On success, OrgForms' mutations switch the active org; bounce to the app. */}
          <OrgForms onSuccess={() => navigate('/objectives')} />
        </section>
      </div>
    </div>
  );
}
