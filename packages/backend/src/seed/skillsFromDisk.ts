import { existsSync, readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import type { BundleManifest, SeedSkillFile } from './skills.js';

/**
 * Read seedable skills from a repo checkout's `catalog/skills` directory —
 * `<name>/SKILL.md` files plus the `bundles.json` manifest. Ported from
 * infra/scripts/seed-skills.mjs so local/dev seeds match the deploy seed.
 * Used by the dev server's offline seed and the new-org starter fallback.
 */

/**
 * Parse `name` + (folded) `description` from a SKILL.md YAML front matter block.
 * The repo skills use `description: >-` folded scalars, so indented continuation
 * lines are gathered until the next top-level key or the closing `---`.
 */
export function parseFrontmatter(md: string): { name?: string; description: string } {
  const lines = md.split(/\r?\n/);
  if (lines[0]?.trim() !== '---') return { name: undefined, description: '' };
  let name: string | undefined;
  const descParts: string[] = [];
  let inDesc = false;
  for (let i = 1; i < lines.length; i++) {
    const line = lines[i] ?? '';
    if (line.trim() === '---') break;
    const top = /^([A-Za-z0-9_-]+):\s?(.*)$/.exec(line);
    if (top && !line.startsWith(' ')) {
      inDesc = false;
      const [, key, value] = top;
      if (key === 'name') name = value!.trim();
      else if (key === 'description') {
        inDesc = true;
        const v = value!.trim();
        if (v && v !== '>-' && v !== '>' && v !== '|' && v !== '|-') descParts.push(v);
      }
      continue;
    }
    if (inDesc && line.trim()) descParts.push(line.trim());
  }
  return { name, description: descParts.join(' ').trim() };
}

/** Every `<dir>/<name>/SKILL.md`, parsed and sorted by skill name. */
export function readSkillFiles(dir: string): SeedSkillFile[] {
  if (!existsSync(dir)) return [];
  const files: SeedSkillFile[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const skillMd = path.join(dir, entry.name, 'SKILL.md');
    if (!existsSync(skillMd)) continue;
    const body = readFileSync(skillMd, 'utf8');
    const { name, description } = parseFrontmatter(body);
    files.push({ name: name ?? entry.name, description, body });
  }
  return files.sort((a, b) => a.name.localeCompare(b.name));
}

/** The `bundles.json` manifest at `manifestPath`; `{}` when absent or unparsable. */
export function readBundleManifest(manifestPath: string): BundleManifest {
  if (!existsSync(manifestPath)) return {};
  try {
    return JSON.parse(readFileSync(manifestPath, 'utf8')) as BundleManifest;
  } catch (err) {
    console.warn(`[seed] could not parse ${manifestPath}; seeding no bundles:`, err);
    return {};
  }
}
