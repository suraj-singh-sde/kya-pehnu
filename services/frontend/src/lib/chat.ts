import { ApiError, errorMessage, type MessageOut, type SessionOut, type ToolCallView } from "@/api/client";

export const MESSAGE_MAX = 4000; // MessageIn.text limit
export const FEEDBACK_REASON_MAX = 500; // FeedbackIn.reason limit
export const ANNOUNCE_MAX = 300; // longest text put in the live region

/** Plain words for an API error, chosen by its `code`; the server's own text is kept where it names the reason. */
export function chatErrorText(e: unknown): string {
  if (!(e instanceof ApiError)) return errorMessage(e);
  switch (e.code) {
    case "no_provider":
      return "No AI provider is set up, so the stylist cannot answer.";
    case "turn_running":
      return "The stylist is still working on this chat. Try again when it has finished.";
    case "proposal_expired":
      return e.message; // "Not done: <reason>. Nothing was changed."
    case "db_busy":
      return "Kya Pehnu is busy right now. Try again in a moment.";
    case "insufficient_storage":
      return "The disk is full, so nothing was saved. Free some space and try again.";
  }
  if (e.status === 404) return "That chat, outfit or proposal no longer exists.";
  return e.message;
}

export const isNoProvider = (e: unknown): boolean => e instanceof ApiError && e.code === "no_provider";

/** One step of a turn: what the model did (tool calls) and then what it said. */
export type Step = { step: number; calls: ToolCallView[]; replies: MessageOut[] };
export type TurnGroup = { turn: number; user: MessageOut[]; steps: Step[]; notices: MessageOut[] };

/**
 * Groups the session view by turn and step, in order. The user's message comes first, each step lists its tool calls
 * before its reply, and notices come last. Calls and replies are only matched by the ids and numbers the server sent.
 */
export function groupTurns({ messages, tool_calls }: SessionOut): TurnGroup[] {
  const turns = new Map<number, TurnGroup>();
  const turnOf = (n: number): TurnGroup => {
    let t = turns.get(n);
    if (!t) turns.set(n, (t = { turn: n, user: [], steps: [], notices: [] }));
    return t;
  };
  const stepOf = (t: TurnGroup, n: number): Step => {
    let s = t.steps.find((x) => x.step === n);
    if (!s) t.steps.push((s = { step: n, calls: [], replies: [] }));
    return s;
  };
  for (const c of tool_calls) stepOf(turnOf(c.turn), c.step).calls.push(c);
  for (const m of messages) {
    const t = turnOf(m.turn);
    if (m.role === "user") t.user.push(m);
    else if (m.role === "notice") t.notices.push(m);
    else stepOf(t, m.step).replies.push(m);
  }
  for (const t of turns.values()) t.steps.sort((a, b) => a.step - b.step);
  return [...turns.values()].sort((a, b) => a.turn - b.turn);
}

/** The newest assistant text or notice, cut for the live region; "" when there is none. */
export function announcement({ messages }: SessionOut): string {
  const last = [...messages].reverse().find((m) => m.role !== "user");
  return last ? last.content.slice(0, ANNOUNCE_MAX) : "";
}

/** The highest turn number in the session, 0 for an empty chat. */
export const latestTurn = (s: SessionOut | undefined): number => s?.messages.reduce((n, m) => Math.max(n, m.turn), 0) ?? 0;

/** A route parameter as a row id, or null when it is not one. */
export const parseSessionId = (raw: string | undefined): number | null => (raw && /^[1-9]\d{0,15}$/.test(raw) ? Number(raw) : null);

type Saved = { seen: number | null; value: number };

/** The rating to show: one saved here wins until the page hands over a different one (a refetch, another tab). */
export function shownRating(rating: number | null | undefined, saved: Saved | null): number | null {
  const current = rating ?? null;
  return saved && saved.seen === current ? saved.value : current;
}

/** Why a pending proposal cannot be confirmed; the server only says `actionable: false`. */
export function blockedReason(proposedOn: string, today: string): string {
  return proposedOn < today ? `Proposed on ${proposedOn}, the day has ended.` : "An item changed or is no longer available.";
}

const TOOL_LABEL: Record<string, string> = {
  search_wardrobe: "Searched the wardrobe",
  get_item: "Looked at an item",
  generate_outfits: "Worked out outfits",
  get_weather: "Checked the weather",
  get_wear_history: "Checked the wear history",
  log_wear: "Proposed logging a wear",
  record_feedback: "Saved your rating",
  update_item_status: "Proposed a status change",
  create_event: "Saved the event",
};
const TOOL_ERROR: Record<string, string> = {
  unknown_tool: "the stylist asked for something that does not exist",
  invalid_arguments: "the request was not valid",
  place_not_found: "place not found",
  not_found: "not found",
  no_alternative: "no other choice fits",
  not_available: "that item is not available to wear",
  limit_reached: "no more outfit searches in this reply",
  internal_error: "the tool failed",
};

/** "Searched the wardrobe: Found 12 items", or "Saved the event: failed, place not found". Unknown names and codes stay readable. */
export function toolLine(name: string, summary: string, error: string | null): string {
  const label = TOOL_LABEL[name] ?? "Used a tool";
  if (error) return `${label}: failed, ${TOOL_ERROR[error] ?? "something went wrong"}`;
  return summary ? `${label}: ${summary}` : label;
}
