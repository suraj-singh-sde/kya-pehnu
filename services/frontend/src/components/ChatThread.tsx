import { useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import type { SessionOut, ToolCallView } from "@/api/client";
import { ChatOutfitCard } from "@/components/ChatOutfitCard";
import { EventChip } from "@/components/EventChip";
import { ProposalCard } from "@/components/ProposalCard";
import { groupTurns, toolLine, type TurnGroup } from "@/lib/chat";

/** All message text is React text: markup and markdown stay literal (NFR-10). */
export function Bubble({ children }: { children: string }) {
  return <p className="msg me">{children}</p>;
}

/** The stylist's mark beside everything it says: a hanger. */
export function Mark() {
  return (
    <span className="mark" aria-hidden="true">
      <svg viewBox="0 0 24 24">
        <path d="M12 4a2 2 0 1 1 2 2c-1 0-2 .8-2 2v1.5M12 9.5 3.5 16a1 1 0 0 0 .6 1.8h15.8a1 1 0 0 0 .6-1.8Z" />
      </svg>
    </span>
  );
}

/** The stylist's turn: the mark, then what it did and said. */
export function Bot({ children, role }: { children: React.ReactNode; role?: string }) {
  return (
    <div className="turn" role={role}>
      <Mark />
      <div className="turn-body">{children}</div>
    </div>
  );
}

/**
 * The looks in a row. On a phone the row swipes one look at a time and a dot per look says which is in view; a tap on a
 * dot scrolls to that look. The dots are hidden on desktop, where the looks are a grid.
 */
function Looks({ count, children }: { count: number; children: React.ReactNode }) {
  const row = useRef<HTMLDivElement>(null);
  const [at, setAt] = useState(0);
  const onScroll = () => {
    const [a, b] = row.current!.children;
    const step = b ? (b as HTMLElement).offsetLeft - (a as HTMLElement).offsetLeft : 1;
    setAt(Math.round(row.current!.scrollLeft / step));
  };
  const go = (i: number) => row.current?.children[i]?.scrollIntoView({ behavior: "smooth", inline: "start", block: "nearest" });
  return (
    <>
      <div ref={row} className="looks" role="list" aria-label={`${count} looks`} onScroll={onScroll}>
        {children}
      </div>
      {count > 1 && (
        <div className="dots">
          {Array.from({ length: count }, (_, i) => (
            <button key={i} type="button" aria-label={`Look ${i + 1} of ${count}`} aria-current={i === at} onClick={() => go(i)} />
          ))}
        </div>
      )}
    </>
  );
}

function CallOutput({ call, sessionId }: { call: ToolCallView; sessionId: number }) {
  const { card, event, proposal } = call;
  return (
    <>
      {event && <EventChip event={event} />}
      {card && (
        <>
          {card.problem && <p className="notice">{card.problem.message}</p>}
          {card.suggestions.length === 0 && !card.problem && <p className="say">Nothing to suggest for this event.</p>}
          {card.suggestions.length > 0 && (
            <Looks count={card.suggestions.length}>
              {card.suggestions.map((s, i) => (
                <ChatOutfitCard key={s.item_ids.join("-")} card={card} suggestion={s} sessionId={sessionId} index={i} />
              ))}
            </Looks>
          )}
        </>
      )}
      {proposal && <ProposalCard proposal={proposal} sessionId={sessionId} />}
    </>
  );
}

/** A turn that showed looks says it all with them: the prose that restates each look under them is left out. */
const showedLooks = (t: TurnGroup): boolean => t.steps.some((s) => s.calls.some((c) => (c.card?.suggestions.length ?? 0) > 0));

/** One turn's reply: what the stylist did (steps), then what it showed and said, in one block per step. */
export function ChatThread({ session }: { session: SessionOut }) {
  const turns = useMemo(() => groupTurns(session), [session]);
  const sessionId = session.session.id;
  if (turns.length === 0) return null;
  return (
    <div className="thread" aria-live="polite">
      {turns.map((t) => (
        <section key={t.turn} className="contents" aria-label={`Turn ${t.turn}`}>
          {t.user.map((m) => (
            <Bubble key={m.id}>{m.content}</Bubble>
          ))}
          {t.steps.map((s) => (showedLooks(t) && s.calls.length === 0 ? null : (
            <Bot key={s.step}>
              {s.calls.length > 0 && (
                <ul className="steps" aria-label="What the stylist did">
                  {s.calls.map((c) => (
                    <li key={c.id} className={`step${c.error ? " bad" : ""}`}>
                      {toolLine(c.name, c.summary, c.error)}
                    </li>
                  ))}
                </ul>
              )}
              {s.calls.map((c) => (
                <CallOutput key={c.id} call={c} sessionId={sessionId} />
              ))}
              {!showedLooks(t) &&
                s.replies.map((m) => (
                  <p key={m.id} className="say">
                    {m.content}
                  </p>
                ))}
            </Bot>
          )))}
          {t.notices.map((m) => (
            <Bot key={m.id}>
              <div className="notice">
                {m.content} <Link className="lnk" to="/settings">Open Account</Link>
              </div>
            </Bot>
          ))}
        </section>
      ))}
    </div>
  );
}
