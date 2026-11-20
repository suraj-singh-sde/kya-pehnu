import { useIsMutating, useMutation, useMutationState, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, NavLink, useNavigate, useParams } from "react-router-dom";
import { api, ApiError, unwrap, type EventType, type SessionOut } from "@/api/client";
import { localToday } from "@/components/outfitHelpers";
import { Bot, Bubble, ChatThread } from "@/components/ChatThread";
import { SavedOutfits } from "@/components/SavedOutfits";
import { dayPart, WeatherLine } from "@/components/WeatherLine";
import { announcement, chatErrorText, isNoProvider, latestTurn, MESSAGE_MAX, parseSessionId } from "@/lib/chat";

const NO_PROVIDER_TEXT = "No AI provider is set up, so the stylist cannot answer.";
const WORKING_TEXT = "Looking through your wardrobe… local models can take a minute.";
const SESSION_STALE_MS = 0; // reopening a chat always rereads it: items may have been deleted or changed elsewhere (H3)
const AFTER_TURN = ["chat-sessions", "events", "outfits", "items"];
const EVENT_TYPES: EventType[] = ["casual", "office", "party", "wedding", "date", "festival", "travel", "gym", "interview"];
const HINT: Record<EventType, string> = {
  casual: "Dinner with friends",
  office: "Client meeting",
  party: "Ananya’s birthday",
  wedding: "Rohan’s sangeet",
  date: "Drinks at eight",
  festival: "Diwali at home",
  travel: "Three days in Goa",
  gym: "Evening run",
  interview: "Second round at a startup",
};
/** Asks a new chat offers to start from; a tap puts one in the box to send or edit. */
const STARTS = ["Dinner with friends tonight", "Office tomorrow, outdoors", "Pack for three days in Goa", "What haven’t I worn lately?"];
const cap = (s: string) => s[0].toUpperCase() + s.slice(1);

type Turn = { text: string; after: number };

/** What the stylist is asked, from the options and the text. A free sentence alone goes as typed. */
/** A chosen day in full, "Saturday 14 November 2026", so the stylist cannot misread it (noon: no day rollover). */
const WHEN = new Intl.DateTimeFormat(undefined, { weekday: "long", day: "numeric", month: "long", year: "numeric" });

/** The ask as sent: the occasion, then the day (the chosen one, or today and its part) and the setting. */
export function compose(type: EventType | null, outdoors: boolean, text: string, when = ""): string {
  const t = text.trim();
  const day = when ? WHEN.format(new Date(`${when}T12:00`)) : "";
  if (!type) return t && day ? `${t} (on ${day})` : t;
  const occasion = t ? `${cap(type)}: ${t}` : `a ${type} occasion`;
  return `Suggest outfits for ${occasion}. ${day ? `On ${day}` : `Today, ${dayPart(new Date())}`}, ${outdoors ? "outdoors" : "indoors"}.`;
}

/** The date a chat was last active, as text (a tooltip is no use on a phone). */
const DATE = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short" });

const useSessions = () => useQuery({ queryKey: ["chat-sessions"], queryFn: () => unwrap(api.GET("/api/chat/sessions")) });

/**
 * The rail beside the conversation: New chat, the chats (newest activity first, as the API orders them) and the saved
 * looks. On a phone it is a drawer that the Chats button in the header slides in.
 */
function Rail({ open, activeId, onClose }: { open: boolean; activeId: number | null; onClose: () => void }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const sessions = useSessions();
  const remove = useMutation({
    mutationFn: (id: number) => unwrap(api.DELETE("/api/chat/sessions/{session_id}", { params: { path: { session_id: id } } })),
    onSuccess: async (_none, id) => {
      qc.removeQueries({ queryKey: ["chat-session", id] });
      await qc.invalidateQueries({ queryKey: ["chat-sessions"] });
      if (id === activeId) navigate("/chat");
    },
  });
  const ask = (id: number, title: string) => {
    if (window.confirm(`Delete the chat "${title}"? Its messages are removed and this cannot be undone.`)) remove.mutate(id);
  };
  const error = remove.error ?? sessions.error;
  return (
    <>
      <div className={`rail-scrim${open ? " on" : ""}`} onClick={onClose} />
      <aside className={`rail${open ? " on" : ""}`} aria-label="Chats and saved looks">
        <NavLink to="/chat" end className="btn primary rail-new" onClick={onClose}>
          New chat
        </NavLink>
        {!!sessions.data?.length && (
          <>
            <h2 className="rail-h">Chats</h2>
            <ol className="chats">
              {sessions.data.map((s) => {
                const title = s.title || "New chat";
                return (
                  <li key={s.id}>
                    <NavLink to={`/chat/${s.id}`} onClick={onClose}>
                      <span className="title">{title}</span>
                      <time dateTime={s.updated_at}>{DATE.format(new Date(s.updated_at))}</time>
                    </NavLink>
                    {s.id === activeId && (
                      <button type="button" className="x" aria-label={`Delete the chat "${title}"`} title="Delete" disabled={remove.isPending} onClick={() => ask(s.id, title)}>
                        ×
                      </button>
                    )}
                  </li>
                );
              })}
            </ol>
          </>
        )}
        {error && (
          <p role="alert" className="err">
            {chatErrorText(error)}
          </p>
        )}
        <SavedOutfits />
      </aside>
    </>
  );
}

/** A new chat's opening: the question, large, and a few asks to start from. */
function Hero({ onPick }: { onPick: (text: string) => void }) {
  const profile = useQuery({ queryKey: ["profile"], queryFn: () => unwrap(api.GET("/api/profile")) });
  const items = useQuery({ queryKey: ["items", "all"], queryFn: () => unwrap(api.GET("/api/items")) });
  const n = items.data?.length;
  const place = profile.data?.home_city;
  return (
    <div className="st-hero">
      <h2>What should I wear?</h2>
      <p>
        Tell me the occasion. I pick from {n ? `the ${n} pieces in your wardrobe` : "your wardrobe"}
        {place ? ` and keep the weather in ${place} in mind.` : "."}
      </p>
      <div className="starts" aria-label="Asks to start from">
        {STARTS.map((t, i) => (
          <button key={t} type="button" style={{ "--i": i } as React.CSSProperties} onClick={() => onPick(t)}>
            {t}
          </button>
        ))}
      </div>
    </div>
  );
}

/**
 * The ask bar: the occasion and setting as one row of options, the question, and Send. It sends one turn at a time; a
 * new chat is started when there is none. `fresh` shows the opening above it until the first ask is on its way.
 */
function AskBar({ id, session, fresh = false }: { id: number | null; session: SessionOut | undefined; fresh?: boolean }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [type, setType] = useState<EventType | null>(null);
  const [outdoors, setOutdoors] = useState(false);
  const [when, setWhen] = useState("");
  const [text, setText] = useState("");
  const [announced, setAnnounced] = useState("");
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => unwrap(api.GET("/api/settings")) });
  const turnKey = id == null ? ["chat-turn"] : ["chat-turn", id];
  const running = useIsMutating({ mutationKey: ["chat-turn"] }) > 0;
  const pending = useMutationState({ filters: { mutationKey: turnKey, status: "pending" }, select: (m) => m.state.variables as Turn | undefined })[0];

  // One turn at a time. The result is shown at once, then the chat is read again, so the server's latest state always wins.
  const send = useMutation({
    mutationKey: ["chat-turn", id ?? "new"],
    mutationFn: async (v: Turn) => {
      const sid = id ?? (await unwrap(api.POST("/api/chat/sessions"))).id;
      const out = await unwrap(api.POST("/api/chat/sessions/{session_id}/messages", { params: { path: { session_id: sid } }, body: { text: v.text } }));
      return { sid, out };
    },
    onSuccess: ({ sid, out }) => {
      qc.setQueryData(["chat-session", sid], out.session);
      if (sid !== id) navigate(`/chat/${sid}`);
    },
    onSettled: (r) => Promise.all([qc.invalidateQueries({ queryKey: ["chat-session", r?.sid ?? id] }), ...AFTER_TURN.map((k) => qc.invalidateQueries({ queryKey: [k] }))]),
  });

  const box = useRef<HTMLInputElement>(null);
  const wasRunning = useRef(false);
  useEffect(() => {
    if (wasRunning.current && !running) box.current?.focus();
    wasRunning.current = running;
  }, [running]);

  const noProvider = settings.data?.llm_configured === false;
  const message = compose(type, outdoors, text, when);
  const blocked = noProvider || running || !message;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (blocked) return;
    send.mutate(
      { text: message, after: latestTurn(session) },
      {
        onSuccess: ({ out }) => {
          setText("");
          setType(null);
          setWhen("");
          setAnnounced(announcement(out.session));
        },
      },
    );
  };
  const showPending = pending && latestTurn(session) <= pending.after;
  const pick = (t: string) => {
    setType(null);
    setText(t);
    box.current?.focus();
  };

  return (
    <>
      {fresh && !showPending && <Hero onPick={pick} />}
      {showPending && (
        <div className="thread">
          <Bubble>{pending.text}</Bubble>
          {running && (
            <Bot role="status">
              <div className="typing">
                <i style={{ "--k": 0 } as React.CSSProperties} />
                <i style={{ "--k": 1 } as React.CSSProperties} />
                <i style={{ "--k": 2 } as React.CSSProperties} />
                <span>{WORKING_TEXT}</span>
              </div>
            </Bot>
          )}
        </div>
      )}
      <form className="askbar" onSubmit={submit} aria-label="Ask the stylist">
        <div className="askbar-in">
          <div className="opts" role="group" aria-label="Occasion and setting">
            {EVENT_TYPES.map((t) => (
              <label key={t} className="opt">
                <input type="radio" name="type" checked={type === t} onChange={() => setType(t)} onClick={() => type === t && setType(null)} />
                <span>{cap(t)}</span>
              </label>
            ))}
            <span className="opt-gap" aria-hidden="true" />
            {[false, true].map((o) => (
              <label key={String(o)} className="opt">
                <input type="radio" name="setting" checked={outdoors === o} onChange={() => setOutdoors(o)} />
                <span>{o ? "Outdoors" : "Indoors"}</span>
              </label>
            ))}
            <span className="opt-gap" aria-hidden="true" />
            {/* a word beside the native picker: an empty date input shows nothing on an iPhone */}
            <label className={`opt-when${when ? " on" : ""}`} title="Pick a day to plan ahead; empty means today">
              <span>{when ? "On" : "Today"}</span>
              <input type="date" aria-label="Which day? Leave empty for today" min={localToday()} value={when} onChange={(e) => setWhen(e.target.value)} />
            </label>
          </div>
          <div className="askrow">
            <input
              id="ask"
              ref={box}
              className="askin"
              aria-label={type ? `What is the ${type} occasion?` : "Ask the stylist"}
              placeholder={type ? HINT[type] : "What should I wear to…"}
              autoComplete="off"
              maxLength={MESSAGE_MAX}
              value={text}
              disabled={running}
              onChange={(e) => setText(e.target.value)}
            />
            <button type="submit" className={`send${running ? " busy" : ""}`} aria-label={running ? "Working" : "Ask"} disabled={blocked}>
              <svg viewBox="0 0 24 24" aria-hidden="true">
                {running ? <path d="M12 3a9 9 0 1 0 9 9" /> : <path d="M12 19V5M5 12l7-7 7 7" />}
              </svg>
            </button>
          </div>
        </div>
        {noProvider && (
          <div className="notice">
            {NO_PROVIDER_TEXT} <Link className="lnk" to="/settings">Open Account</Link>
          </div>
        )}
        {send.error && (
          <p role="alert" className="err">
            {chatErrorText(send.error)}{" "}
            {isNoProvider(send.error) && (
              <Link className="lnk" to="/settings">Open Account</Link>
            )}
          </p>
        )}
      </form>
      <div className="sr-only" role="status" aria-live="polite">
        {announced}
      </div>
    </>
  );
}

function ChatPane({ id }: { id: number }) {
  const session = useQuery({
    queryKey: ["chat-session", id],
    queryFn: () => unwrap(api.GET("/api/chat/sessions/{session_id}", { params: { path: { session_id: id } } })),
    staleTime: SESSION_STALE_MS,
  });
  const end = useRef<HTMLDivElement>(null);
  const size = (session.data?.messages.length ?? 0) + (session.data?.tool_calls.length ?? 0);
  // Braces matter: newer Chrome returns a promise from scrollIntoView, and React calls whatever an effect returns.
  useEffect(() => {
    end.current?.scrollIntoView?.({ block: "end" });
  }, [size]);
  const missing = session.error instanceof ApiError && session.error.status === 404;

  return (
    <>
      {session.isLoading && <p className="sub">Loading…</p>}
      {session.error && !session.data && (
        <p role="alert" className="err">
          {missing ? "This chat no longer exists." : chatErrorText(session.error)}{" "}
          {missing && (
            <Link className="lnk" to="/chat">Start a new one</Link>
          )}
        </p>
      )}
      {session.data && <ChatThread session={session.data} />}
      <div ref={end} />
      {!missing && <AskBar id={id} session={session.data} />}
    </>
  );
}

export default function Chat() {
  const { sessionId } = useParams();
  const id = parseSessionId(sessionId);
  const [rail, setRail] = useState(false);
  const sessions = useSessions();
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && setRail(false);
    addEventListener("keydown", k);
    return () => removeEventListener("keydown", k);
  }, []);
  return (
    <div className="studio">
      <Rail open={rail} activeId={id} onClose={() => setRail(false)} />
      <div className="convo">
        <header className="st-head">
          <div>
            <h1>Stylist</h1>
            <WeatherLine />
          </div>
          <button type="button" className="mw-icon ph" aria-label={`Chats and saved looks${sessions.data?.length ? `, ${sessions.data.length} chats` : ""}`} onClick={() => setRail(true)}>
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M4 6h16M4 12h10M4 18h13" />
            </svg>
            {!!sessions.data?.length && <span className="mw-dot">{sessions.data.length}</span>}
          </button>
          <Link className="mw-icon add ph" to="/chat" aria-label="New chat">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M12 5v14M5 12h14" />
            </svg>
          </Link>
        </header>
        {id != null ? (
          <ChatPane key={id} id={id} />
        ) : (
          <>
            {sessionId && <p className="err">There is no chat at this address.</p>}
            <AskBar id={null} session={undefined} fresh />
          </>
        )}
      </div>
    </div>
  );
}
