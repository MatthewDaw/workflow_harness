import { describe, it, expect } from 'vitest';
import {
  definitionOfDoneSchema,
  orgConfigSchema,
  DEFAULT_DEFINITION_OF_DONE,
  CONTROL_ACTIONS,
  controlActionSchema,
  controlFrameSchema,
  mcpServerSchema,
  MCP_TRANSPORTS,
  projectSchema,
  agentSchema,
  skillSchema,
  truePointerSchema,
  variantIdFor,
  normalizeEnabledEntry,
  enabledEntryName,
  enabledEntryVariant,
} from '../src/dto.js';

const orgScope = { tier: 'org', id: 'acme' } as const;

/**
 * The org-wide Definition of Done (plan-mapping feature 1). Advisory config that
 * `/update-progress` reports against; it has a sensible floor as defaults.
 */
describe('definitionOfDoneSchema', () => {
  it('applies the org-wide floor as defaults on an empty object', () => {
    const dod = definitionOfDoneSchema.parse({});
    expect(dod.requiresUnitTests).toBe(true); // floor: unit tests
    expect(dod.requiresProdE2E).toBe(false); // tighten-only opt-in
    expect(dod.notes).toBeUndefined();
  });

  it('round-trips a tightened DoD (prod-E2E + notes)', () => {
    const dod = definitionOfDoneSchema.parse({
      requiresUnitTests: true,
      requiresProdE2E: true,
      notes: 'Run npm run e2e:prod against the deployed env.',
    });
    expect(dod).toEqual({
      requiresUnitTests: true,
      requiresProdE2E: true,
      notes: 'Run npm run e2e:prod against the deployed env.',
    });
  });

  it('rejects a non-boolean flag', () => {
    expect(definitionOfDoneSchema.safeParse({ requiresUnitTests: 'yes' }).success).toBe(false);
  });

  it('DEFAULT_DEFINITION_OF_DONE matches the parsed defaults', () => {
    expect(definitionOfDoneSchema.parse({})).toEqual(DEFAULT_DEFINITION_OF_DONE);
  });
});

/**
 * Control actions are the shared source of truth for HQ → daemon steering: the
 * web sender, the REST/WS control routes, and the wrapper receiver all key off
 * this enum. `shutdown`/`kill` were added so a live session can be terminated
 * from HQ (graceful, with a force fallback).
 */
describe('controlActionSchema', () => {
  it('includes inject/pause/interrupt and the terminate actions shutdown/kill', () => {
    expect(CONTROL_ACTIONS).toEqual(['inject', 'pause', 'interrupt', 'shutdown', 'kill']);
    for (const a of ['shutdown', 'kill'] as const) {
      expect(controlActionSchema.safeParse(a).success).toBe(true);
    }
  });

  it('rejects an unknown action', () => {
    expect(controlActionSchema.safeParse('restart').success).toBe(false);
  });

  it('parses a shutdown control frame with an empty payload default', () => {
    const frame = controlFrameSchema.parse({ sessionId: 's1', action: 'shutdown' });
    expect(frame).toEqual({ sessionId: 's1', action: 'shutdown', payload: {} });
  });
});

/**
 * MCP servers are the structured catalog record (a `transport` discriminated
 * union), the third pillar alongside skills/agents. stdio defaults args/env;
 * http/sse require a valid url; an unknown transport fails the union.
 */
describe('mcpServerSchema', () => {
  it('exposes the supported transports', () => {
    expect(MCP_TRANSPORTS).toEqual(['stdio', 'http', 'sse']);
  });

  it('parses a valid stdio server and defaults args/env when omitted', () => {
    const server = mcpServerSchema.parse({
      name: 'fs',
      scope: orgScope,
      transport: 'stdio',
      command: 'npx',
    });
    expect(server).toEqual({
      name: 'fs',
      scope: orgScope,
      transport: 'stdio',
      command: 'npx',
      args: [],
      env: {},
    });
  });

  it('round-trips a stdio server with args + env', () => {
    const server = mcpServerSchema.parse({
      name: 'fs',
      scope: orgScope,
      transport: 'stdio',
      command: 'npx',
      args: ['-y', '@modelcontextprotocol/server-filesystem', '/tmp'],
      env: { TOKEN: 'secret' },
    });
    expect(server).toMatchObject({
      transport: 'stdio',
      args: ['-y', '@modelcontextprotocol/server-filesystem', '/tmp'],
      env: { TOKEN: 'secret' },
    });
  });

  it('parses a valid http server and defaults headers when omitted', () => {
    const server = mcpServerSchema.parse({
      name: 'remote',
      scope: orgScope,
      transport: 'http',
      url: 'https://mcp.example.com/sse',
    });
    expect(server).toEqual({
      name: 'remote',
      scope: orgScope,
      transport: 'http',
      url: 'https://mcp.example.com/sse',
      headers: {},
    });
  });

  it('rejects an http server with a non-URL url', () => {
    const result = mcpServerSchema.safeParse({
      name: 'remote',
      scope: orgScope,
      transport: 'http',
      url: 'not-a-url',
    });
    expect(result.success).toBe(false);
  });

  it('rejects an unknown transport', () => {
    const result = mcpServerSchema.safeParse({
      name: 'bad',
      scope: orgScope,
      transport: 'websocket',
      url: 'https://mcp.example.com',
    });
    expect(result.success).toBe(false);
  });

  it('rejects a stdio server missing its command', () => {
    expect(
      mcpServerSchema.safeParse({ name: 'fs', scope: orgScope, transport: 'stdio' }).success,
    ).toBe(false);
  });
});

/**
 * Back-compat: project/agent records written before MCP servers existed lack the
 * new attachment arrays; `.default([])` keeps them valid and fills the field.
 */
describe('MCP server attachment back-compat', () => {
  it('defaults enabledMcpServers to [] for a legacy project', () => {
    const project = projectSchema.parse({
      id: 'p1',
      name: 'Weekly Compass',
      repo: 'gh/acme/weekly-compass',
      ownerUserId: 'u1',
    });
    expect(project.enabledMcpServers).toEqual([]);
  });

  it('defaults mcpServers to [] for a legacy agent', () => {
    const agent = agentSchema.parse({
      name: 'planner',
      scope: orgScope,
      model: 'claude-opus-4-8',
    });
    expect(agent.mcpServers).toEqual([]);
  });
});

/**
 * VERSIONING MODEL (KTD6). The variant/revision fields are MIXED INTO skill /
 * agent / mcpServer and are all optional/back-compat: a legacy record (no
 * version fields) still validates and gains NO fabricated fields.
 */
describe('versioning fields (back-compat)', () => {
  it('a legacy skill (no version fields) still validates and stays minimal', () => {
    const skill = skillSchema.parse({
      name: 'reconcile',
      scope: orgScope,
      kind: 'skill',
    });
    // No version field is fabricated on a legacy record.
    expect(skill.version).toBeUndefined();
    expect(skill.variantId).toBeUndefined();
    expect(skill.baseName).toBeUndefined();
    expect(skill.repoId).toBeUndefined();
    expect(skill.files).toBeUndefined();
  });

  it('round-trips a forked skill variant with version fields + whole-dir files', () => {
    const skill = skillSchema.parse({
      name: 'reconcile',
      scope: orgScope,
      kind: 'skill',
      baseName: 'reconcile',
      variantId: 'reconcile#R#weekly-compass#U#matt',
      repoId: 'weekly-compass',
      authorUserId: 'matt',
      version: 3,
      createdAt: 1717200000000,
      files: { 'SKILL.md': '# Reconcile', 'scripts/run.sh': 'echo hi' },
    });
    expect(skill.version).toBe(3);
    expect(skill.variantId).toBe('reconcile#R#weekly-compass#U#matt');
    expect(skill.files).toEqual({ 'SKILL.md': '# Reconcile', 'scripts/run.sh': 'echo hi' });
  });

  it('a legacy agent / mcp server validates without version fields', () => {
    const agent = agentSchema.parse({ name: 'planner', scope: orgScope, model: 'opus' });
    expect(agent.version).toBeUndefined();
    const mcp = mcpServerSchema.parse({
      name: 'fs',
      scope: orgScope,
      transport: 'stdio',
      command: 'npx',
    });
    expect((mcp as { version?: number }).version).toBeUndefined();
  });

  it('rejects a non-positive version', () => {
    expect(
      skillSchema.safeParse({ name: 's', scope: orgScope, kind: 'skill', version: 0 }).success,
    ).toBe(false);
  });
});

describe('variantIdFor', () => {
  it('returns the baseName itself for the base variant (no repo + no user)', () => {
    expect(variantIdFor('reconcile')).toBe('reconcile');
    expect(variantIdFor('reconcile', undefined, undefined)).toBe('reconcile');
  });

  it('appends repo + user for a fork', () => {
    expect(variantIdFor('reconcile', 'weekly-compass', 'matt')).toBe(
      'reconcile#R#weekly-compass#U#matt',
    );
  });
});

describe('truePointerSchema', () => {
  it('parses a pointer with an explicit rev', () => {
    expect(truePointerSchema.parse({ baseName: 's', variantId: 's', rev: 2 })).toEqual({
      baseName: 's',
      variantId: 's',
      rev: 2,
    });
  });

  it('allows an absent rev (means latest)', () => {
    const p = truePointerSchema.parse({ baseName: 's', variantId: 's' });
    expect(p.rev).toBeUndefined();
  });
});

describe('enabled-set entry pins (U-Ver-Pin, back-compat)', () => {
  it('normalizes a bare-string entry (no variant pin)', () => {
    expect(normalizeEnabledEntry('reconcile')).toEqual({ name: 'reconcile' });
    expect(enabledEntryName('reconcile')).toBe('reconcile');
    expect(enabledEntryVariant('reconcile')).toBeUndefined();
  });

  it('normalizes an object entry carrying a variantId', () => {
    const entry = { name: 'reconcile', variantId: 'reconcile#R#r#U#u' };
    expect(normalizeEnabledEntry(entry)).toEqual(entry);
    expect(enabledEntryName(entry)).toBe('reconcile');
    expect(enabledEntryVariant(entry)).toBe('reconcile#R#r#U#u');
  });
});

describe('orgConfigSchema', () => {
  it('accepts an empty config (dod optional)', () => {
    expect(orgConfigSchema.parse({})).toEqual({});
  });

  it('embeds a Definition of Done', () => {
    const cfg = orgConfigSchema.parse({ dod: { requiresProdE2E: true } });
    expect(cfg.dod).toEqual({ requiresUnitTests: true, requiresProdE2E: true });
  });
});
