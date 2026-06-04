import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MarkdownView } from './MarkdownView.js';

describe('MarkdownView (U9)', () => {
  it('renders GFM headings, lists, and links', () => {
    render(
      <MarkdownView markdown={'# Goal\n\nShip it.\n\n- one\n- two\n\n[docs](https://x.test)'} />,
    );
    expect(screen.getByRole('heading', { name: 'Goal' })).toBeInTheDocument();
    expect(screen.getByText('Ship it.')).toBeInTheDocument();
    expect(screen.getByText('one')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'docs' })).toHaveAttribute('href', 'https://x.test');
  });

  it('renders GFM task lists as ☑ / ☐ glyphs', () => {
    const { container } = render(
      <MarkdownView markdown={'- [x] done item\n- [ ] todo item'} />,
    );
    const text = container.textContent ?? '';
    expect(text).toContain('☑');
    expect(text).toContain('☐');
    expect(text).toContain('done item');
    expect(text).toContain('todo item');
    // The raw checkbox <input> is replaced by a glyph span.
    expect(container.querySelector('input[type="checkbox"]')).toBeNull();
  });

  it('renders a GFM table', () => {
    render(
      <MarkdownView markdown={'| a | b |\n| - | - |\n| 1 | 2 |'} />,
    );
    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'a' })).toBeInTheDocument();
  });

  it('renders a raw-HTML body (our converted .html docs)', () => {
    render(<MarkdownView markdown={'<h2>Hello</h2><p>world</p>'} />);
    expect(screen.getByRole('heading', { name: 'Hello' })).toBeInTheDocument();
    expect(screen.getByText('world')).toBeInTheDocument();
  });

  it('strips frontmatter from an HTML-body doc', () => {
    const { container } = render(
      <MarkdownView markdown={'---\ncompletion: 5\n---\n<h2>Hi</h2>'} />,
    );
    expect(screen.getByRole('heading', { name: 'Hi' })).toBeInTheDocument();
    expect(container.textContent ?? '').not.toContain('completion: 5');
  });

  it('sanitizes dangerous raw HTML (strips event handlers / scripts)', () => {
    const { container } = render(
      <MarkdownView markdown={'before <img src=x onerror="alert(1)"> after'} />,
    );
    // rehype-sanitize strips the event handler but keeps inert markup safe.
    expect(container.querySelector('img')?.getAttribute('onerror')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect(container.textContent).toContain('after');
  });
});
