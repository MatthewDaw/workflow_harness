import { describe, it, expect } from 'vitest';
import { extractCompliance } from './compliance.js';

describe('extractCompliance', () => {
  it('extracts the report and removes the block (and sentinels) from the body', () => {
    const content =
      '<!--hq:compliance v1-->## Compliance breakdown\n\nlooks good\n<!--/hq:compliance-->\n# Goal\n\nbody';
    const { report, body } = extractCompliance(content);
    expect(report).toBe('## Compliance breakdown\n\nlooks good');
    expect(body).not.toContain('hq:compliance');
    expect(body).not.toContain('Compliance breakdown');
    expect(body).toContain('# Goal');
    expect(body).toContain('body');
  });

  it('returns null report and unchanged body when there is no block', () => {
    const content = '# Goal\n\nNo compliance here.';
    const { report, body } = extractCompliance(content);
    expect(report).toBeNull();
    expect(body).toBe(content);
  });

  it('handles an HTML-bodied block', () => {
    const content =
      '# Goal\n\n<!-- hq:compliance -->\n<table><tr><td>Source Code</td><td>met</td></tr></table>\n<!-- /hq:compliance -->\n\nrest';
    const { report, body } = extractCompliance(content);
    expect(report).toBe('<table><tr><td>Source Code</td><td>met</td></tr></table>');
    expect(body).not.toContain('hq:compliance');
    expect(body).not.toContain('<table>');
    expect(body).toContain('# Goal');
    expect(body).toContain('rest');
  });
});
