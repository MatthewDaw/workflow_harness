import { NavLink, Outlet, useParams } from 'react-router-dom';

/**
 * Project hub layout (U19/U21): renders the project sub-nav promoted from the
 * wireframe's `.subnav` and an <Outlet> for the active sub-tab. Sub-tabs (U12):
 * Project Requirements / Detailed Requirements / Weekly / Sessions / Agents.
 * Project Requirements is the default landing sub-tab.
 */
const SUBTABS = [
  // U12: two-tier requirements sub-tabs. Project Requirements is the default.
  { to: 'requirements', label: 'Project Requirements' },
  { to: 'detailed-requirements', label: 'Detailed Requirements' },
  { to: 'weekly', label: 'Weekly' },
  { to: 'sessions', label: 'Sessions' },
  { to: 'skills', label: 'Skills' },
  { to: 'mcp-servers', label: 'MCP Servers' },
  { to: 'agents', label: 'Agents' },
];

export function ProjectLayout() {
  const { projectId } = useParams();

  return (
    <div>
      <nav className="hq-subnav" aria-label="Project sections">
        <span className="px-1 py-2.5 text-xs text-faint">Projects ›&nbsp;</span>
        {SUBTABS.map((t) => (
          <NavLink
            key={t.label}
            to={t.to}
            className={({ isActive }) => (isActive ? 'on' : undefined)}
          >
            {t.label}
          </NavLink>
        ))}
        <span className="ml-auto px-1 py-2.5 font-mono text-[11px] text-faint">{projectId}</span>
      </nav>
      <Outlet />
    </div>
  );
}
