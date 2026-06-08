import { useState, type FormEvent } from 'react';
import { useApproveDeviceMutation } from '../../api/baseApi.js';
import { ScreenHeader } from '../../components/primitives.js';

/**
 * Get started: install `claude+`, then link this device (device-auth approval).
 *
 * The install commands are the real ones from `scripts/install.sh` and the
 * `claude-plus` npm package, so a brand-new user can go from zero to a linked
 * terminal on one screen. Below them, a signed-in HQ user pastes the user code
 * shown by `claude+ login` in their terminal and approves it; on success the
 * terminal's poll loop receives a token, on a miss we tell them the code is gone.
 */

const RELEASES_URL = 'https://github.com/workflow-harness/claude-plus/releases';

/** The command-line ways to install `claude+`, sourced from the real installer
 * (the "download a release" link is rendered separately below). */
const INSTALL_OPTIONS: { label: string; command: string }[] = [
  {
    label: 'curl (macOS / Linux)',
    command:
      'curl -fsSL https://raw.githubusercontent.com/workflow-harness/claude-plus/main/scripts/install.sh | sh',
  },
  { label: 'npm (any platform)', command: 'npm i -g claude-plus' },
];

/** One install command with a Copy button. Copy degrades to a no-op when the
 * Clipboard API is unavailable; the command stays visible and selectable. */
function InstallCommand({ label, command }: { label: string; command: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard?.writeText(command);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard blocked/unsupported: leave the text for manual selection.
    }
  }

  return (
    <div className="hq-box bg-paper mt-2">
      <div className="text-[11px] uppercase tracking-wide text-faint">{label}</div>
      <div className="mt-1.5 flex items-center gap-2">
        <code className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap font-mono text-[13px]">
          {command}
        </code>
        <button type="button" className="hq-btn shrink-0 text-[11px]" onClick={() => void copy()}>
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
    </div>
  );
}

export function LinkDevice() {
  const [code, setCode] = useState('');
  const [approve, { isLoading }] = useApproveDeviceMutation();
  const [result, setResult] = useState<'approved' | 'notfound' | 'error' | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setResult(null);
    try {
      const res = await approve({ userCode: code.trim() }).unwrap();
      setResult(res.approved ? 'approved' : 'notfound');
    } catch {
      setResult('error');
    }
  }

  return (
    <div className="hq-pad" data-testid="link-device">
      <ScreenHeader
        title="Get started"
        subtitle="Linking connects a terminal running claude+ to Command HQ, so its sessions show up here — live, watchable, and steerable."
      />

      {/* What linking is + the 3 steps at a glance */}
      <div className="hq-box bg-paper">
        <div className="text-[13px] text-mut">
          “Linking a device” pairs one computer’s <code className="font-mono">claude+</code> with
          your HQ account. You do it once per machine. Three steps:
        </div>
        <ol className="mt-2 list-decimal space-y-1 pl-5 text-[13px]">
          <li>
            Install <code className="font-mono">claude+</code> on that machine.
          </li>
          <li>
            Run <code className="font-mono">claude+ login</code> there — it shows a short code and
            waits.
          </li>
          <li>
            Type that code into the box below and approve. Done — sessions start streaming here.
          </li>
        </ol>
      </div>

      {/* 1. Install claude+ */}
      <section aria-labelledby="install-claude-plus" className="mt-6">
        <h3 id="install-claude-plus" className="text-sm font-semibold">
          1 · Install claude+
        </h3>
        <p className="mt-1 text-[13px] text-mut">
          On the machine you want to link. Pick one — installs the{' '}
          <code className="font-mono">claude-plus</code> binary (aliased{' '}
          <code className="font-mono">claude+</code>). The machine also needs the real{' '}
          <code className="font-mono">claude</code> CLI on its PATH.
        </p>
        {INSTALL_OPTIONS.map((opt) => (
          <InstallCommand key={opt.label} label={opt.label} command={opt.command} />
        ))}
        <div className="hq-box bg-paper mt-2">
          <div className="text-[11px] uppercase tracking-wide text-faint">
            Or download a release
          </div>
          <a
            className="mt-1.5 block font-mono text-[13px]"
            href={RELEASES_URL}
            target="_blank"
            rel="noreferrer"
          >
            github.com/workflow-harness/claude-plus/releases →
          </a>
        </div>
      </section>

      {/* 2. Get a code from the terminal */}
      <section aria-labelledby="run-login" className="mt-6">
        <h3 id="run-login" className="text-sm font-semibold">
          2 · Run claude+ login
        </h3>
        <p className="mt-1 text-[13px] text-mut">
          In that machine’s terminal, run <code className="font-mono">claude+ login</code>. It
          prints a short code and waits for you to approve it here:
        </p>
        <pre className="hq-box bg-ink mt-2 overflow-x-auto whitespace-pre font-mono text-[12px] text-paper">
          {`$ claude+ login
To finish signing in, open Command HQ and approve this code:

    WDJB-MJXT

Waiting for approval… (Ctrl-C to cancel)`}
        </pre>
        <p className="mt-2 text-[13px] text-mut">
          Leave that terminal running — it keeps waiting until you approve the code in step 3.
        </p>
      </section>

      {/* 3. Approve the code here */}
      <section aria-labelledby="link-a-device" className="mt-6">
        <h3 id="link-a-device" className="text-sm font-semibold">
          3 · Approve the code here
        </h3>
        <p className="mt-1 text-[13px] text-mut">
          Type the code your terminal showed (e.g. <code className="font-mono">WDJB-MJXT</code>) and
          approve. That links the machine to your account and its terminal finishes logging in.
        </p>
        <form className="hq-box bg-paper mt-2" onSubmit={onSubmit}>
          <label
            htmlFor="device-user-code"
            className="block text-[11px] uppercase tracking-wide text-faint"
          >
            User code (from your terminal)
          </label>
          <input
            id="device-user-code"
            className="hq-input mt-1.5 w-full max-w-[260px] font-mono tracking-wider uppercase"
            placeholder="WDJB-MJXT"
            autoComplete="off"
            value={code}
            onChange={(e) => setCode(e.target.value)}
          />
          <div className="mt-2 flex gap-2">
            <button
              type="submit"
              className="hq-btn hq-btn-pri"
              disabled={isLoading || code.trim().length === 0}
            >
              {isLoading ? 'Approving…' : 'Approve device'}
            </button>
          </div>

          {result === 'approved' && (
            <>
              <div
                className="mt-3 text-[13px] text-good"
                data-testid="link-device-result"
                role="status"
              >
                Device approved — return to your terminal.
              </div>
              <div className="mt-1 text-[13px] text-mut">
                Its login completes automatically and sessions begin streaming. Open{' '}
                <strong>Sessions</strong> to watch them.
              </div>
            </>
          )}
          {result === 'notfound' && (
            <div
              className="mt-3 text-[13px] text-mut"
              data-testid="link-device-result"
              role="status"
            >
              That code wasn&rsquo;t found or has expired.
            </div>
          )}
          {result === 'error' && (
            <div
              className="mt-3 text-[13px] text-mut"
              data-testid="link-device-result"
              role="status"
            >
              Something went wrong. Check the code and try again.
            </div>
          )}
        </form>
      </section>

      {/* Day-to-day usage: run a session, detach/reattach, quit. The chords mirror
          the claude+ chrome (Ctrl-G prefix), and the quit path emits a done for each
          session so the Sessions list here clears immediately. */}
      <section aria-labelledby="use-claude-plus" className="mt-8 border-t border-line pt-6">
        <h3 id="use-claude-plus" className="text-sm font-semibold">
          Using claude+ in a project
        </h3>
        <p className="mt-1 text-[13px] text-mut">
          Once a machine is linked, run <code className="font-mono">claude+</code> in any repo to
          start a session there — it streams into <strong>Sessions</strong> here, live and
          steerable. Add <code className="font-mono">--dangerously-skip-permissions</code> to skip
          Claude&rsquo;s per-action approval prompts. Each session runs in a background daemon, so
          you can leave and reattach without losing it.
        </p>

        <div className="hq-box bg-paper mt-3">
          <div className="text-[11px] uppercase tracking-wide text-faint">
            In-session commands — press Ctrl-G, then the key
          </div>
          <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1.5 text-[13px]">
            <dt className="font-mono text-ink">Ctrl-G d</dt>
            <dd className="min-w-0 text-mut">
              <strong>Detach</strong> — leave the session running and drop back to your shell.
            </dd>

            <dt className="font-mono text-ink">claude+</dt>
            <dd className="min-w-0 text-mut">
              <strong>Reattach</strong> — run <code className="font-mono">claude+</code> again in the
              same folder to pick the session back up.
            </dd>

            <dt className="font-mono text-ink">Ctrl-G q</dt>
            <dd className="min-w-0 text-mut">
              <strong>Quit</strong> — end every session and stop the daemon; the rows clear from{' '}
              <strong>Sessions</strong> here right away.
            </dd>

            <dt className="font-mono text-ink">Ctrl-G c</dt>
            <dd className="min-w-0 text-mut">
              <strong>New session</strong> in the same project.
            </dd>

            <dt className="font-mono text-ink">Ctrl-G n / p / 1-5</dt>
            <dd className="min-w-0 text-mut">
              Switch session tabs — next, previous, or jump to one.
            </dd>
          </dl>
        </div>

        <p className="mt-2 text-[13px] text-mut">
          You can also end a session from here: open <strong>Sessions</strong> and use{' '}
          <strong>Shut Down</strong> on its row.
        </p>
      </section>

      {/* Terminal command reference — run in a normal shell (not inside a session),
          distinct from the in-session Ctrl-G chords above. login + reset are the
          recovery commands, so they get plain-language descriptions. */}
      <section aria-labelledby="claude-plus-commands" className="mt-8 border-t border-line pt-6">
        <h3 id="claude-plus-commands" className="text-sm font-semibold">
          Terminal commands
        </h3>
        <p className="mt-1 text-[13px] text-mut">
          Run these in a normal terminal (not inside a session). They&rsquo;re the verbs and flags
          of the <code className="font-mono">claude+</code> CLI.
        </p>

        <div className="hq-box bg-paper mt-3">
          <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1.5 text-[13px]">
            <dt className="font-mono text-ink">claude+</dt>
            <dd className="min-w-0 text-mut">
              Start a session in the current folder — or reattach to the one already running there.
            </dd>

            <dt className="font-mono text-ink">claude+ --dangerously-skip-permissions</dt>
            <dd className="min-w-0 text-mut">
              Same, but the daemon skips Claude&rsquo;s per-action approval prompts. Pick one mode
              and stick with it — switching restarts that folder&rsquo;s daemon.
            </dd>

            <dt className="font-mono text-ink">claude+ login</dt>
            <dd className="min-w-0 text-mut">
              Sign this machine in to HQ. Prints a short code to approve above (step 3). Run it in a
              plain terminal so the code is visible.
            </dd>

            <dt className="font-mono text-ink">claude+ ls</dt>
            <dd className="min-w-0 text-mut">
              List the sessions running across all your folders, with an index for{' '}
              <code className="font-mono">--session</code>.
            </dd>

            <dt className="font-mono text-ink">claude+ --session=N</dt>
            <dd className="min-w-0 text-mut">
              Attach to the session at index <code className="font-mono">N</code> from{' '}
              <code className="font-mono">claude+ ls</code>.
            </dd>

            <dt className="font-mono text-ink">claude+ sync</dt>
            <dd className="min-w-0 text-mut">
              Sync everything this project uses — skills, agents, and MCP servers —
              down from HQ (and up from the repo&rsquo;s own <code className="font-mono">.claude</code>).
              Run it after connecting a project or enabling something in HQ.
            </dd>

            <dt className="font-mono text-ink">claude+ stop N</dt>
            <dd className="min-w-0 text-mut">
              Stop the session at index <code className="font-mono">N</code> from{' '}
              <code className="font-mono">claude+ ls</code>. Its conversations resume on the
              next launch in that folder.
            </dd>

            <dt className="font-mono text-ink">claude+ reset</dt>
            <dd className="min-w-0 text-mut">
              Clear all daemon state if a session gets stuck or shows an &ldquo;incompatible
              build&rdquo; error. Your conversations are preserved and resume on the next launch.
            </dd>
          </dl>
        </div>
      </section>
    </div>
  );
}
