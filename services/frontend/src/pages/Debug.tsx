import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError, errorMessage, unwrap } from "@/api/client";
import { Badge } from "@/components/ui/badge";

const selectCls = "h-9 rounded-md border bg-background px-2 text-sm";
const FORBIDDEN = 403;
const HAS_ZONE = /(Z|[+-]\d\d:\d\d)$/;

/** SQLite hands back naive UTC datetimes; read a zone-less string as UTC, leave a zoned one alone. */
const when = (iso: string): string => new Date(HAS_ZONE.test(iso) ? iso : `${iso}Z`).toLocaleString();

/** The session filter box as a row id; anything else (empty, 0, 1.5) means "all sessions". */
const sessionFilter = (raw: string): number | undefined => (/^[1-9]\d{0,15}$/.test(raw) ? Number(raw) : undefined);

/** A failed log read: the plain 403 text, or the server's message. */
function LogError({ error }: { error: unknown }) {
  return error instanceof ApiError && error.status === FORBIDDEN ? (
    <p className="text-sm text-muted-foreground">
      Admins only. The LLM call log holds raw prompts and replies, so only the admin of this Kya Pehnu can open it.
    </p>
  ) : (
    <p className="text-sm text-destructive">{errorMessage(error)}</p>
  );
}

function ToolCalls({ sessionId }: { sessionId: number | undefined }) {
  const calls = useQuery({
    retry: false,
    queryKey: ["tool-calls", sessionId],
    queryFn: () => unwrap(api.GET("/api/debug/tool-calls", { params: { query: { session_id: sessionId } } })),
  });
  return (
    <section className="space-y-2" aria-labelledby="tool-calls">
      <h2 id="tool-calls" className="text-lg font-semibold">
        Tool calls
      </h2>
      {calls.isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {calls.error && <LogError error={calls.error} />}
      {calls.data?.length === 0 && <p className="text-sm text-muted-foreground">No tool calls yet.</p>}
      {calls.data?.map((c) => (
        <details key={c.id ?? `${c.session_id}-${c.call_id}`} className="rounded-md border p-3 text-sm">
          <summary className="flex cursor-pointer flex-wrap items-center gap-2">
            <span className="text-muted-foreground">{c.created_at ? when(c.created_at) : ""}</span>
            <span>
              session {c.session_id} · turn {c.turn}
            </span>
            <Badge variant="secondary">{c.name}</Badge>
            <span>{c.latency_ms} ms</span>
            {c.error && <Badge variant="destructive">{c.error}</Badge>}
          </summary>
          <div className="mt-2 space-y-2">
            <p className="font-medium">Arguments</p>
            <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded bg-muted p-2">{JSON.stringify(c.args ?? null, null, 2)}</pre>
            <p className="font-medium">Result{c.truncated && " (cut)"}</p>
            <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded bg-muted p-2">{c.result}</pre>
          </div>
        </details>
      ))}
    </section>
  );
}

export default function Debug() {
  const [purpose, setPurpose] = useState("");
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [session, setSession] = useState("");
  const sessionId = sessionFilter(session.trim());
  const calls = useQuery({
    retry: false, // a 403 is final: the log is only served to the computer running Kya Pehnu
    queryKey: ["llm-calls", purpose, errorsOnly, sessionId],
    queryFn: () =>
      unwrap(
        api.GET("/api/debug/llm-calls", { params: { query: { purpose: purpose || undefined, errors_only: errorsOnly, session_id: sessionId } } }),
      ),
  });

  const forbidden = calls.error instanceof ApiError && calls.error.status === FORBIDDEN;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <select className={selectCls} value={purpose} onChange={(e) => setPurpose(e.target.value)} aria-label="Purpose">
          <option value="">All purposes</option>
          <option value="tagging">tagging</option>
          <option value="dedupe">dedupe</option>
          <option value="test">test</option>
          <option value="vision_check">vision_check</option>
          <option value="clean_picture">clean_picture</option>
          <option value="chat">chat</option>
          <option value="outfit_pick">outfit_pick</option>
        </select>
        <label className="flex items-center gap-2 text-sm">
          Session
          <input
            className={`${selectCls} w-24`}
            type="number"
            min={1}
            step={1}
            inputMode="numeric"
            placeholder="all"
            aria-label="Session id"
            value={session}
            onChange={(e) => setSession(e.target.value)}
          />
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={errorsOnly} onChange={(e) => setErrorsOnly(e.target.checked)} /> errors only
        </label>
      </div>
      {calls.error && <LogError error={calls.error} />}
      {calls.data?.length === 0 && <p className="text-sm text-muted-foreground">No LLM calls yet.</p>}
      <div className="space-y-2">
        {calls.data?.map((c) => (
          <details key={c.id} className="rounded-md border p-3 text-sm">
            <summary className="flex cursor-pointer flex-wrap items-center gap-2">
              <span className="text-muted-foreground">{c.created_at ? when(c.created_at) : ""}</span>
              <Badge variant="secondary">{c.purpose}</Badge>
              <span>{c.model}</span>
              <span>{c.latency_ms} ms</span>
              <span>
                {c.input_tokens ?? "?"}→{c.output_tokens ?? "?"} tok
              </span>
              {c.error && <Badge variant="destructive">error</Badge>}
            </summary>
            <div className="mt-2 space-y-2">
              {c.error && <pre className="whitespace-pre-wrap text-destructive">{c.error}</pre>}
              <p className="font-medium">Request</p>
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded bg-muted p-2">{c.request_summary}</pre>
              <p className="font-medium">Response</p>
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded bg-muted p-2">{c.response}</pre>
            </div>
          </details>
        ))}
      </div>
      {!forbidden && <ToolCalls sessionId={sessionId} />} {/* the 403 text above covers both logs */}
    </div>
  );
}
