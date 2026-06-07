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

describe('orgConfigSchema', () => {
  it('accepts an empty config (dod optional)', () => {
    expect(orgConfigSchema.parse({})).toEqual({});
  });

  it('embeds a Definition of Done', () => {
    const cfg = orgConfigSchema.parse({ dod: { requiresProdE2E: true } });
    expect(cfg.dod).toEqual({ requiresUnitTests: true, requiresProdE2E: true });
  });
});
