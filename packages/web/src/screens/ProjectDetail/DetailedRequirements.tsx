import { useEffect, useMemo, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import {
  useGetProjectDocsQuery,
  useGetProjectDocContentQuery,
  type ProjectDoc,
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
 * Instead of a single selected doc, the reading pane is a CONTINUOUS scroll of
 * every doc stacked vertically (each a `<section>` fetching its own markdown). An
 * IntersectionObserver tracks whichever section is at the top of the viewport and
 * sets it as the active doc — driving BOTH the top "Active doc · … · NN%" bar and
 * the sidebar highlight. Clicking a sidebar doc scrolls its section into view.
 * Read-only.
 */
export function DetailedRequirements() {
  const { projectId = '' } = useParams();
  const { data: docs, isLoading: docsLoading } = useGetProjectDocsQuery(projectId, {
    skip: !projectId,
  });
  const docList = useMemo(() => docs ?? [], [docs]);

  const tree = useMemo(() => buildDocTree(docList), [docList]);

  // Active doc tracked by the scroll-spy; null until the first intersection (or
  // when there are no docs). Falls back to the first doc for the initial render.
  const [activePath, setActivePath] = useState<string | null>(null);
  const effectiveActive = activePath ?? docList[0]?.path ?? null;
  const activeDoc = docList.find((d) => d.path === effectiveActive) ?? null;

  // Refs to each section, keyed by doc path, so the observer + click-to-scroll
  // can reach them.
  const sectionRefs = useRef<Map<string, HTMLElement>>(new Map());

  // Overall fallback = mean of all docs (initial render, before any intersect).
  const overallPct =
    docList.length > 0
      ? Math.round(docList.reduce((a, d) => a + d.completion, 0) / docList.length)
      : 0;

  // Scroll-spy: observe every section and set the top-most visible one active.
  useEffect(() => {
    if (docList.length === 0) return;
    const observer = new IntersectionObserver(
      (entries) => {
        // Pick the visible entry nearest the top of the viewport.
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        const top = visible[0];
        if (top) {
          const path = (top.target as HTMLElement).dataset.path;
          if (path) setActivePath(path);
        }
      },
      { rootMargin: '0px 0px -70% 0px', threshold: 0 },
    );
    for (const el of sectionRefs.current.values()) observer.observe(el);
    return () => observer.disconnect();
  }, [docList]);

  // The bar/label reflects the active doc (or the overall fallback).
  const activePct = activeDoc?.completion ?? overallPct;
  const activeLabel = activeDoc
    ? `Active doc · ${activeDoc.title} · ${activePct}%`
    : `Overall completion · ${overallPct}%`;

  function selectDoc(path: string) {
    setActivePath(path);
    sectionRefs.current.get(path)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

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
                const isActive = d.path === effectiveActive;
                return (
                  <li key={d.path}>
                    <button
                      type="button"
                      style={indent}
                      onClick={() => selectDoc(d.path)}
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
            {docList.length === 0 ? (
              <div className="text-mut">No requirement docs.</div>
            ) : (
              docList.map((d) => (
                <DocSection
                  key={d.path}
                  projectId={projectId}
                  doc={d}
                  registerRef={(el) => {
                    if (el) sectionRefs.current.set(d.path, el);
                    else sectionRefs.current.delete(d.path);
                  }}
                />
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * One doc rendered in the continuous scroll: a `<section>` with a heading and
 * its markdown, fetched independently. The `data-path` attribute lets the
 * scroll-spy observer map an intersecting section back to its doc path.
 */
function DocSection({
  projectId,
  doc,
  registerRef,
}: {
  projectId: string;
  doc: ProjectDoc;
  registerRef: (el: HTMLElement | null) => void;
}) {
  const { data: content, isFetching } = useGetProjectDocContentQuery(
    { projectId, path: doc.path },
    { skip: !projectId || !doc.path },
  );
  return (
    <section
      ref={registerRef}
      data-path={doc.path}
      aria-label={doc.title}
      className="scroll-mt-4 border-b border-paper2 pb-4 pt-2 first:pt-0 last:border-b-0 last:pb-0"
    >
      <h3 className="m-0 mb-2 text-[13px] font-semibold text-ink">{doc.title}</h3>
      {isFetching ? (
        <div className="text-mut">Loading…</div>
      ) : (
        <MarkdownView markdown={content?.markdown ?? ''} />
      )}
    </section>
  );
}
