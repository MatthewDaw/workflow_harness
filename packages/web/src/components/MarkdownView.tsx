import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

/**
 * MarkdownView (U9): renders GitHub-Flavored Markdown for the requirements
 * surfaces. GFM gives us tables, strikethrough, and — crucially for the
 * requirements model — task lists (`- [x] done` / `- [ ] todo`), which we
 * render as ☑ / ☐ glyphs so a requirements doc reads like a checklist.
 *
 * Safety: react-markdown does NOT pass raw HTML through by default (we do not
 * enable `rehype-raw`), so embedded HTML in the source is rendered as inert
 * text. That keeps HQ-owned and repo-sourced markdown sanitized without an
 * extra sanitizer pass.
 */
export function MarkdownView({ markdown }: { markdown: string }) {
  return (
    <div className="hq-markdown" data-testid="markdown-view">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // Render GFM task-list checkboxes as static glyphs (the underlying
          // <input type="checkbox"> is read-only/disabled by remark-gfm).
          input: ({ checked, type }) =>
            type === 'checkbox' ? (
              <span aria-hidden="true" className="hq-task-box">
                {checked ? '☑' : '☐'}
              </span>
            ) : null,
        }}
      >
        {markdown}
      </ReactMarkdown>
    </div>
  );
}
