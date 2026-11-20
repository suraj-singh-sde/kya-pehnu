import type { Event } from "@/api/client";

/** "Dinner, Oct 10, 2026, 8:00 PM, Delhi"; an event without a title shows its type. */
function eventLabel(e: Event): string {
  const when = new Date(e.starts_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  return [e.title || e.type, when, e.place].filter(Boolean).join(", ");
}

export function EventChip({ event }: { event: Event }) {
  return <p className="okline">Event saved: {eventLabel(event)}</p>;
}
