import createClient from "openapi-fetch";
import type { components, paths } from "./types";

export const api = createClient<paths>({ baseUrl: "" });

const SIGN_IN_PATH = "/signin";

/** The sign-in page, remembering where the person was so they land back there. */
export const signInUrl = (): string => `${SIGN_IN_PATH}?next=${encodeURIComponent(location.pathname + location.search)}`;

// A 401 anywhere means the session is gone (or never existed): go and sign in. The sign-in page asks "who am I?"
// itself, so it is left alone.
api.use({
  onResponse({ response }) {
    if (response.status === 401 && location.pathname !== SIGN_IN_PATH) location.assign(signInUrl());
    return undefined;
  },
});

type S = components["schemas"];
export type Item = S["Item"];
export type ItemDetail = S["ItemDetail"];
export type Piece = S["IngestPiece"];
export type Job = S["IngestJob"];
export type LlmCall = S["LlmCall"];
export type Profile = S["UserProfile"];
export type SettingsInfo = S["SettingsOut"];
export type LlmView = S["LlmView"];
export type PresetView = S["PresetView"];
export type Event = S["Event"];
export type EventType = S["EventType"];
export type OutfitOut = S["OutfitOut"];
export type SuggestionOut = S["SuggestionOut"];
export type WeatherOut = S["WeatherOut"];
export type Problem = S["Problem"];
export type SuggestIn = S["SuggestIn"];
export type SessionSummary = S["SessionSummary"];
export type SessionOut = S["SessionOut"];
export type MessageOut = S["MessageOut"];
export type ToolCallView = S["ToolCallView"];
export type OutfitsCard = S["OutfitsCard"];
export type CardSuggestion = S["CardSuggestion"];
export type ProposalView = S["ProposalView"];
export type TurnOut = S["TurnOut"];
export type FeedbackIn = S["FeedbackIn"];
export type FeedbackOut = S["FeedbackOut"];
export type ToolCall = S["ToolCall"];
export type MeOut = S["MeOut"];
export type WeatherReport = S["WeatherReport"];

/** The server's message from an error body `{detail, code, request_id}`; FastAPI 422s carry a list of `{msg}`. */
export function errorText(e: unknown): string {
  if (e && typeof e === "object" && "detail" in e) {
    const d = (e as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) return d.map((x) => (x && typeof x === "object" && "msg" in x ? String(x.msg) : JSON.stringify(x))).join("; ");
    return JSON.stringify(d);
  }
  return e ? String(e) : "";
}

/** The machine-readable `code` of an error body (absent on FastAPI's own 422s). */
function errorCode(e: unknown): string | undefined {
  const c = e && typeof e === "object" && "code" in e ? (e as { code: unknown }).code : undefined;
  return typeof c === "string" ? c : undefined;
}

/** An error response; `status` lets a page tell "not allowed" from "broken", `code` which rule failed (e.g. "place_not_found"). */
export class ApiError extends Error {
  status: number;
  code?: string;
  constructor(message: string, status: number, code?: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

/** Resolve an openapi-fetch result to its data, or throw with the server's `detail` message. */
export async function unwrap<T>(p: Promise<{ data?: T; error?: unknown; response: Response }>): Promise<T> {
  const { data, error, response } = await p;
  if (!response.ok) throw new ApiError(errorText(error) || response.statusText, response.status, errorCode(error));
  return data as T;
}

/** Message of a caught value, for display. */
export const errorMessage = (e: unknown): string => (e instanceof Error ? e.message : String(e));

/** Stored paths are relative to the data dir (e.g. "images/thumb/x.webp"); the API serves them at "/images/...". */
export const imageUrl = (path?: string | null): string => (path ? `/${path}` : "");
