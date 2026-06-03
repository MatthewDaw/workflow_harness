import { NavLink, Outlet, useParams } from 'react-router-dom';

/**
 * Project hub layout (U19/U21): renders the project sub-nav promoted from the
 * wireframe's `.subnav` and an <Outlet> for the active sub-tab. Sub-tabs:
 * Overview / Weekly / Sessions / Agents.
 */
const SUBTABS = [
  { to: '', label: 'Overview', end: true },
  { to: 'weekly', label: 'Weekly' },
  { to: 'sessions', label: 'Sessions' },
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
            end={t.end}
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
