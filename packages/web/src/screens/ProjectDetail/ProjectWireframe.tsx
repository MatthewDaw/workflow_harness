import { useParams } from 'react-router-dom';
import { useGetProjectWireframeQuery } from '../../api/baseApi.js';

/**
 * Preview of the project's `docs/wireframe.html`, shown on the Project
 * Requirements tab. The wireframe is a full HTML document, so the preview renders
 * it inside a sandboxed iframe; clicking the tile or the button opens the raw
 * wireframe as a standalone document in a new browser tab — no Command HQ chrome,
 * just the wireframe. Renders nothing when the repo has no wireframe so the tab
 * stays clean.
 */
export function WireframePreview() {
  const { projectId = '' } = useParams();
  const { data } = useGetProjectWireframeQuery(projectId, { skip: !projectId });
  const html = data?.html ?? '';
  if (!html) return null;

  // The HTML is already in hand (the iframe's srcDoc source), so we serve it from
  // a blob URL; the JSON-wrapped API endpoint can't be opened directly and would
  // require auth. Scripts still run in the new tab (a blob URL inherits the app
  // origin), so the wireframe stays fully interactive.
  const openInNewTab = () => {
    const url = URL.createObjectURL(new Blob([html], { type: 'text/html' }));
    window.open(url, '_blank', 'noopener,noreferrer');
    // Revoke after the tab has had time to load — an immediate revoke would break
    // it; a delayed revoke avoids leaking the blob for the rest of the session.
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  };

  return (
    <div className="mb-3.5 hq-box bg-paper" data-testid="wireframe-preview">
      <div className="mb-1.5 flex items-center justify-between gap-3">
        <div className="text-[11px] uppercase tracking-wide text-faint">
          Wireframe (from GitHub docs/wireframe.html)
        </div>
        <button
          type="button"
          onClick={openInNewTab}
          className="hq-btn shrink-0"
          data-testid="wireframe-open-newtab"
        >
          ↗ Open in new tab
        </button>
      </div>
      <button
        type="button"
        onClick={openInNewTab}
        className="group relative block h-[220px] w-full overflow-hidden rounded border border-line text-left"
        aria-label="Open the wireframe in a new tab"
      >
        {/* The preview is a scaled-down snapshot; the overlay swallows clicks so
            the whole tile behaves as a single button. The container height matches
            the scaled iframe (550px × 0.4 = 220px) so the tile is only as tall as
            the visible snapshot, with no empty space below. allow-scripts lets the
            wireframe's own nav/screen script run. */}
        <iframe
          title="Wireframe preview"
          srcDoc={html}
          sandbox="allow-scripts"
          tabIndex={-1}
          aria-hidden="true"
          className="pointer-events-none origin-top-left"
          style={{ width: '250%', height: '550px', transform: 'scale(0.4)' }}
        />
        <span className="absolute inset-0" />
      </button>
    </div>
  );
}
