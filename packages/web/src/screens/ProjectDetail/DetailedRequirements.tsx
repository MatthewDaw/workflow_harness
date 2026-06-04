import { useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import {
  useGetProjectDocsQuery,
  useGetProjectDocContentQuery,
} from '../../api/baseApi.js';
import { Bar, ScreenHeader } from '../../components/primitives.js';
import { MarkdownView } from '../../components/MarkdownView.js';
import { buildDocTree } from '../../lib/docTree.js';

/**
 * Detailed Requirements sub-tab (U11): the LOW-LEVEL, repo-sourced requirement
 * docs. The sidebar renders a HIERARCHICAL doc tree (grouped by directory under
 * `docs/plans/`, see lib/docTree.ts) where each folder shows an aggregate (mean)
 * completion badge and each doc keeps its own `completion` badge.
 *
 * One doc is shown at a time: clicking a doc in the sidebar selects it, and the
 * reading pane renders just that doc's markdown. The top bar reflects the
 * selected doc's completion. Read-only.
 */
export function DetailedRequirements() {
  const { projectId = '' } = useParams();
  const { data: docs, isLoading: docsLoading } = useGetProjectDocsQuery(projectId, {
    skip: !projectId,
  });
  const docList = useMemo(() => docs ?? [], [docs]);
  const tree = useMemo(() => buildDocTree(docList), [docList]);

  // Selected doc; defaults to the first doc until the user clicks another.
  const [selected, setSelected] = useState<string | null>(null);
  const activePath = selected ?? docList[0]?.path ?? null;
  const activeDoc = docList.find((d) => d.path === activePath) ?? null;

  const { data: content, isFetching: contentFetching } = useGetProjectDocContentQuery(
    { projectId, path: activePath ?? '' },
    { skip: !projectId || !activePath },
  );

  // The bar reflects the selected doc's completion.
  const activePct = activeDoc?.completion ?? 0;
  const activeLabel = activeDoc
    ? `${activeDoc.title} · ${activePct}%`
    : 'Detailed requirements';

  return (
    <div className="hq-pad" data-testid="detailed-requirements">
      <ScreenHeader
        title="Detailed Requirements"
        subtitle="Repo-sourced requirement docs (read-only)."
      />

      <div className="mb-3.5 hq-box bg-paper">
        <div className="text-[11px] uppercase tracking-wide text-faint">{activeLabel}</div>
        <div className="mt-1.5">
          <Bar pct={activePct} />
        </div>
      </div>

      <div className="flex gap-3.5">
        <aside className="w-64 shrink-0" aria-label="Requirement documents">
          <div className="hq-box bg-paper">
            <div className="mb-1.5 text-[11px] uppercase tracking-wide text-faint">Documents</div>
            {docsLoading && <div className="text-mut">Loading…</div>}
            {!docsLoading && docList.length === 0 && (
              <div className="text-mut">No requirement docs.</div>
            )}
            <ul className="m-0 list-none p-0">
              {tree.map((node) => {
                const indent = { paddingLeft: `${node.depth * 12 + 8}px` };
                if (node.kind === 'folder') {
                  return (
                    <li key={`folder:${node.key}`}>
                      <div
                        style={indent}
                        className="flex items-center justify-between gap-2 py-1.5 pr-2 text-[11px] font-semibold uppercase tracking-wide text-faint"
                      >
                        <span className="truncate">{node.label}</span>
                        <span className="shrink-0 font-mono text-[11px] text-faint">
                          {node.completion}%
                        </span>
                      </div>
                    </li>
                  );
                }
                const d = node.doc;
                const isActive = d.path === activePath;
                return (
                  <li key={d.path}>
                    <button
                      type="button"
                      style={indent}
                      onClick={() => setSelected(d.path)}
                      aria-current={isActive ? 'true' : undefined}
                      className={`flex w-full items-center justify-between gap-2 rounded-sm py-1.5 pr-2 text-left text-[12.5px] ${
                        isActive ? 'bg-paper2 font-semibold text-ink' : 'text-mut'
                      }`}
                    >
                      <span className="truncate">{d.title}</span>
                      <span className="shrink-0 font-mono text-[11px] text-faint">
                        {d.completion}%
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        </aside>

        <div className="min-w-0 flex-1">
          <div className="hq-box bg-paper">
            {!activePath ? (
              <div className="text-mut">Select a document.</div>
            ) : contentFetching ? (
              <div className="text-mut">Loading…</div>
            ) : (
              <MarkdownView markdown={content?.markdown ?? ''} />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
