import { useState } from 'react';
import { useParams } from 'react-router-dom';
import {
  useGetProjectDocsQuery,
  useGetProjectDocContentQuery,
} from '../../api/baseApi.js';
import { Bar, ScreenHeader } from '../../components/primitives.js';
import { MarkdownView } from '../../components/MarkdownView.js';

/**
 * Detailed Requirements sub-tab (U11): the LOW-LEVEL, repo-sourced requirement
 * docs. A doc-tree sidebar lists every doc from GET /projects/:id/docs with a
 * per-doc `completion` badge; selecting one renders its markdown (fetched from
 * the content endpoint) in the reading pane. The overall progress bar tracks
 * the top doc's completion. Read-only.
 */
export function DetailedRequirements() {
  const { projectId = '' } = useParams();
  const { data: docs, isLoading: docsLoading } = useGetProjectDocsQuery(projectId, {
    skip: !projectId,
  });
  const docList = docs ?? [];
  const [selected, setSelected] = useState<string | null>(null);
  const activePath = selected ?? docList[0]?.path ?? null;

  const { data: content, isFetching: contentFetching } = useGetProjectDocContentQuery(
    { projectId, path: activePath ?? '' },
    { skip: !projectId || !activePath },
  );

  // Overall bar = the top (first) doc's completion.
  const overallPct = docList[0]?.completion ?? 0;

  return (
    <div className="hq-pad" data-testid="detailed-requirements">
      <ScreenHeader
        title="Detailed Requirements"
        subtitle="Repo-sourced requirement docs (read-only)."
      />

      <div className="mb-3.5 hq-box bg-paper">
        <div className="text-[11px] uppercase tracking-wide text-faint">
          Overall completion · {overallPct}%
        </div>
        <div className="mt-1.5">
          <Bar pct={overallPct} />
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
              {docList.map((d) => {
                const isActive = d.path === activePath;
                return (
                  <li key={d.path}>
                    <button
                      type="button"
                      onClick={() => setSelected(d.path)}
                      aria-current={isActive ? 'true' : undefined}
                      className={`flex w-full items-center justify-between gap-2 rounded-sm px-2 py-1.5 text-left text-[12.5px] ${
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
