import { Navigate, Route, Routes } from 'react-router-dom';
import { AppShell } from '../components/AppShell.js';
import { Objectives } from '../screens/Objectives/Objectives.js';
import { Projects } from '../screens/Projects/Projects.js';
import { ProjectLayout } from '../screens/ProjectDetail/ProjectLayout.js';
import {
  ProjectRequirements,
  ProjectRequirementsFull,
} from '../screens/ProjectDetail/ProjectRequirements.js';
import { DetailedRequirements } from '../screens/ProjectDetail/DetailedRequirements.js';
import { ProjectWeekly } from '../screens/ProjectDetail/ProjectWeekly.js';
import { ProjectSessions } from '../screens/ProjectDetail/ProjectSessions.js';
import { ProjectAgents } from '../screens/ProjectDetail/ProjectAgents.js';
import { Sessions } from '../screens/Sessions/Sessions.js';
import { LiveWatch } from '../screens/LiveWatch/LiveWatch.js';
import { Agents } from '../screens/Agents/Agents.js';
import { AgentEditor } from '../screens/Agents/AgentEditor.js';
import { Skills } from '../screens/Skills/Skills.js';
import { SkillBundle } from '../screens/Skills/SkillBundle.js';
import { Weekly } from '../screens/Weekly/Weekly.js';
import { LinkDevice } from '../screens/LinkDevice/LinkDevice.js';

/**
 * Route table mirroring wireframe.html (U19). Objectives is the first nav item
 * and the default landing route. Projects has the Requirements/Weekly/Sessions/
 * Agents sub-tabs; Sessions deep-links into the live watch surface.
 */
export function AppRoutes() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<Navigate to="/objectives" replace />} />
        <Route path="objectives" element={<Objectives />} />

        <Route path="projects" element={<Projects />} />
        <Route path="projects/:projectId" element={<ProjectLayout />}>
          {/* Project Requirements is the default landing sub-tab. */}
          <Route index element={<Navigate to="requirements" replace />} />
          {/* U12: two-tier requirements routes. */}
          <Route path="requirements" element={<ProjectRequirements />} />
          <Route path="requirements/full" element={<ProjectRequirementsFull />} />
          <Route path="detailed-requirements" element={<DetailedRequirements />} />
          <Route path="weekly" element={<ProjectWeekly />} />
          <Route path="sessions" element={<ProjectSessions />} />
          <Route path="agents" element={<ProjectAgents />} />
        </Route>

        <Route path="sessions" element={<Sessions />} />
        <Route path="sessions/:sessionId" element={<LiveWatch />} />

        <Route path="agents" element={<Agents />} />
        <Route path="agents/new" element={<AgentEditor />} />
        <Route path="agents/:name/edit" element={<AgentEditor />} />
        <Route path="skills" element={<Skills />} />
        <Route path="skills/:bundleName" element={<SkillBundle />} />
        <Route path="weekly" element={<Weekly />} />
        <Route path="link-device" element={<LinkDevice />} />

        <Route path="*" element={<Navigate to="/objectives" replace />} />
      </Route>
    </Routes>
  );
}
