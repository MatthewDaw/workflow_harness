import { useEffect } from 'react';
import { NavLink, Outlet } from 'react-router-dom';
import { useDispatch, useSelector } from 'react-redux';
import { useAuth } from '../auth/AuthProvider.js';
import { wsConnect, wsDisconnect } from '../ws/liveActions.js';
import type { RootState } from '../app/store.js';
import { Emblem } from './Emblem.js';
import { OrgSwitcher } from './OrgSwitcher.js';

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
  { to: '/mcp-servers', label: 'MCP Servers' },
  { to: '/link-device', label: 'Get started' },
];

export function AppShell() {
  const { user, signOut } = useAuth();
  const dispatch = useDispatch();
  const token = useSelector((s: RootState) => s.auth.idToken);

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
              <OrgSwitcher />
              <span aria-hidden className="text-cream/30">
                /
              </span>
              <span className="text-cream/80" title="Signed in">
                {user?.username ?? 'me'}
              </span>
              <button
                type="button"
                onClick={() => void signOut()}
                className="hq-btn text-[11px]"
                title="Sign out"
              >
                Sign out
              </button>
            </span>
          </nav>
          <Outlet />
        </div>
      </div>
    </div>
  );
}
