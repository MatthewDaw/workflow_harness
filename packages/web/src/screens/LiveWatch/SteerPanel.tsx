import { useState } from 'react';
import { useSendControlMutation } from '../../api/baseApi.js';

/**
 * The steer box: inject a message, pause, or interrupt the live session by
 * posting control frames through the same gateway the terminal uses.
 */
export function SteerPanel({ sessionId, canSteer }: { sessionId: string; canSteer: boolean }) {
  const [sendControl] = useSendControlMutation();
  const [message, setMessage] = useState('');
  const [sent, setSent] = useState<string | null>(null);
  const [steerError, setSteerError] = useState<string | null>(null);

  // Run a control action and report the REAL outcome. The control POST can fail
  // (502 "daemon offline" when the owning daemon isn't connected, or 500 when the
  // backend can't reach the WS management API) — surface that instead of always
  // claiming success, which previously made a no-op send look like it had worked.
  const runControl = async (action: 'inject' | 'pause' | 'interrupt', text?: string) => {
    setSteerError(null);
    try {
      await sendControl({ sessionId, action, text }).unwrap();
      return true;
    } catch (err) {
      const status = (err as { status?: number | string })?.status;
      setSteerError(
        status === 502
          ? 'daemon offline — the session’s claude+ isn’t connected'
          : `couldn’t reach the session (${status ?? 'error'})`,
      );
      return false;
    }
  };

  return (
    <div className="hq-box mb-3 bg-paper">
      <div className="text-[11px] uppercase tracking-wide text-faint">Steer</div>
      <textarea
        aria-label="Inject a message into the live session"
        value={message}
        onChange={(e) => setMessage(e.target.value)}
        placeholder="type a message to inject into the live session…"
        disabled={!canSteer}
        className="my-2 min-h-[54px] w-full rounded-md border border-line p-2 text-xs disabled:opacity-50"
      />
      <div className="flex gap-1.5">
        <button
          type="button"
          className="hq-btn hq-btn-pri"
          disabled={!canSteer || message.trim() === ''}
          onClick={() => {
            const text = message;
            void runControl('inject', text).then((okSend) => {
              if (okSend) {
                setSent(text);
                setMessage('');
              }
            });
          }}
        >
          send
        </button>
        <button
          type="button"
          className="hq-btn"
          disabled={!canSteer}
          onClick={() => {
            void runControl('pause');
          }}
        >
          ⏸ pause
        </button>
        <button
          type="button"
          className="hq-btn"
          disabled={!canSteer}
          onClick={() => {
            void runControl('interrupt');
          }}
        >
          ⤓ interrupt
        </button>
      </div>
      {steerError && (
        <div className="mt-2 text-[11px] text-live" role="alert">
          {steerError}
        </div>
      )}
      {sent && !steerError && (
        <div className="mt-2 text-[11px] text-good" role="status">
          injected: {sent}
        </div>
      )}
    </div>
  );
}
