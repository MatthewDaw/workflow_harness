import { describe, it, expect } from 'vitest';
import { stripFrontmatter, parseCompletion } from './frontmatter.js';

describe('stripFrontmatter', () => {
  it('removes a leading --- frontmatter block', () => {
    const md = '---\ncompletion: 88\nstatus: active\n---\n\n# Title\n\nbody';
    expect(stripFrontmatter(md)).toBe('# Title\n\nbody');
  });

  it('leaves markdown without frontmatter untouched', () => {
    const md = '# Title\n\nno frontmatter here';
    expect(stripFrontmatter(md)).toBe(md);
  });

  it('does not strip a --- that is not at the very start (e.g. a thematic break)', () => {
    const md = '# Title\n\n---\n\nmore';
    expect(stripFrontmatter(md)).toBe(md);
  });
});

describe('parseCompletion', () => {
  it('reads completion: 88 from frontmatter', () => {
    expect(parseCompletion('---\ncompletion: 88\n---\n# x')).toBe(88);
  });

  it('tolerates quotes and a percent sign', () => {
    expect(parseCompletion('---\ncompletion: "88%"\n---\n# x')).toBe(88);
  });

  it('clamps out-of-range values to 0..100', () => {
    expect(parseCompletion('---\ncompletion: 150\n---')).toBe(100);
    expect(parseCompletion('---\ncompletion: -5\n---')).toBe(0);
  });

  it('returns undefined with no frontmatter or no completion key', () => {
    expect(parseCompletion('# x\n\nbody')).toBeUndefined();
    expect(parseCompletion('---\nstatus: active\n---\n# x')).toBeUndefined();
  });
});
