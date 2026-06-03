import { useEffect } from 'react';
import { NavLink, Outlet } from 'react-router-dom';
import { useDispatch, useSelector } from 'react-redux';
import { useAuth } from '../auth/AuthProvider.js';
import { useGetSessionsQuery } from '../api/baseApi.js';
import { wsConnect, wsDisconnect } from '../ws/liveActions.js';
import type { RootState } from '../app/store.js';
import { Emblem } from './Emblem.js';

/**
 * The Command HQ chrome: brand + top nav, promoted from wireframe.html's
 * `.appnav`. Nav order mirrors the prototype — Objectives is the first item.
 */
const NAV = [
  { to: '/objectives', label: 'Objectives' },
  { to: '/projects', label: 'Projects' },
  { to: '/sessions', label: 'Sessions' },
  { to: '/agents', label: 'Agents' },
  { to: '/skills', label: 'Skills' },
  { to: '/link-device', label: 'Link Device' },
];

export function AppShell() {
  const { user, signOut } = useAuth();
  const dispatch = useDispatch();
  const token = useSelector((s: RootState) => s.auth.idToken);
  const { data: sessions } = useGetSessionsQuery({ live: true });
  const liveCount = sessions?.length ?? 0;

  // Open the live WebSocket once the user is authenticated (H3). Without this the
  // socket never opens, so live watch/steer/counters never update. The token
  // rides the handshake query string so the WS authorizer accepts the connection.
  useEffect(() => {
    if (!user) return;
    const url = import.meta.env.VITE_WS_URL;
    if (!url) return;
    dispatch(wsConnect({ url, token }));
    return () => {
      dispatch(wsDisconnect());
    };
  }, [user, token, dispatch]);

  return (
    <div className="min-h-screen bg-bg">
      <div className="mx-auto max-w-[1180px] px-7 pb-24 pt-6">
        <div className="hq-frame overflow-hidden">
          <nav className="hq-appnav" aria-label="Primary">
            <span className="mr-3.5 flex items-center gap-2">
              <Emblem size={26} />
              <span className="font-stencil text-[15px] font-bold uppercase tracking-wide text-cream">
                Command HQ
              </span>
            </span>
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) => (isActive ? 'on' : undefined)}
              >
                {item.label}
              </NavLink>
            ))}
            <span className="ml-auto flex items-center gap-2.5 text-xs text-cream/70">
              <span className="hq-dot hq-dot-live" />
              {liveCount} live ·{' '}
              <button
                type="button"
                onClick={() => void signOut()}
                className="cursor-pointer text-cream/80 hover:text-cream"
                title="Sign out"
              >
                @{user?.username ?? 'me'}
              </button>
            </span>
          </nav>
          <Outlet />
        </div>
      </div>
    </div>
  );
}
