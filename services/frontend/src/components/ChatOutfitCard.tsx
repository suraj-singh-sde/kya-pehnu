import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError, errorMessage, unwrap, type CardSuggestion, type Item, type OutfitsCard } from "@/api/client";
import { MannequinChat, MannequinView, useMannequin, useMannequinMode } from "@/components/MannequinPicture";
import { Collage, localToday, NOTE_TEXT, WEAR_REASON_MAX, wornOn } from "@/components/outfitHelpers";
import { Thumbs } from "@/components/Thumbs";
import { ItemPic } from "@/components/wardrobe/GarmentIcons";
import { chatErrorText } from "@/lib/chat";

const GONE = "Item no longer available";
const REFRESH = ["outfits", "items", "item"];

type Props = { card: OutfitsCard; suggestion: CardSuggestion; sessionId: number; index?: number };

function swapErrorText(e: unknown): string {
  if (e instanceof ApiError && e.code === "no_alternative") return e.message; // names the slot: "No other bottom fits this event"
  if (e instanceof ApiError && e.status === 404) return "This outfit's event or an item no longer exists.";
  return errorMessage(e);
}

/**
 * One look of the stylist's, as the session view describes it: items may be gone or unavailable since it was proposed.
 * Swaps happen on this card only (the chat keeps the original); Wear this logs what is on the card now.
 */
export function ChatOutfitCard({ card, suggestion, sessionId, index = 0 }: Props) {
  const qc = useQueryClient();
  const { mode } = useMannequinMode();
  const event = card.event;
  // Every item this card has shown, so a swap never offers one back.
  const [shown, setShown] = useState(() => new Set(suggestion.item_ids));
  const [items, setItems] = useState<(Item | null)[]>(suggestion.items);
  const [fresh, setFresh] = useState<number | null>(null);
  const [reason, setReason] = useState(suggestion.reason);
  const [dismissed, setDismissed] = useState(false);
  const itemIds = items.map((i) => i?.id).filter((id): id is number => id != null);
  const anyGone = items.some((i) => i === null);
  const names = items.map((i) => i?.name ?? GONE).join(", ");
  const weather = card.weather;
  const notes = [...new Set([...(weather?.notes ?? []), ...suggestion.notes.map((c) => NOTE_TEXT[c] ?? c)])];

  const swap = useMutation({
    mutationFn: (item: Item) =>
      unwrap(api.POST("/api/outfits/swap", { body: { event_id: event!.id!, item_ids: itemIds, item_id: item.id!, exclude_item_ids: [...shown] } })),
    onSuccess: (next) => {
      setShown((prev) => new Set([...prev, ...next.item_ids]));
      setFresh(next.item_ids.find((id) => !itemIds.includes(id)) ?? null);
      setItems(next.items);
      setReason(next.reason);
      wear.reset();
    },
  });
  const wear = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/outfits/wear", {
          body: { item_ids: itemIds, event_id: event?.id, worn_on: event ? wornOn(event, localToday()) : undefined, reason: reason.slice(0, WEAR_REASON_MAX) || null },
        }),
      ),
    onSuccess: () =>
      Promise.all([...REFRESH.map((k) => qc.invalidateQueries({ queryKey: [k] })), qc.invalidateQueries({ queryKey: ["chat-session", sessionId] })]),
  });
  const laundry = useMutation({
    mutationFn: () => unwrap(api.POST("/api/items/status", { body: { item_ids: itemIds, status: "laundry" } })),
    onSuccess: () => Promise.all(["items", "item"].map((k) => qc.invalidateQueries({ queryKey: [k] }))),
  });

  const mannequin = useMannequin(itemIds, anyGone ? "off" : mode);
  const busy = swap.isPending || wear.isPending;
  const saved = wear.isSuccess;
  const canWear = suggestion.wearable && !!event && !anyGone;
  const canSwap = !!event?.id && !saved;

  return (
    <article className="look" role="listitem" style={{ "--i": index } as React.CSSProperties} aria-label={`Look ${index + 1}: ${names}`}>
      <div className="look-pic">
        <span className="look-n" aria-hidden="true">
          {index + 1}
        </span>
        <MannequinView m={mannequin} fallback={<Collage items={items} fresh={fresh} />} alt={`On a mannequin: ${names}`} className="look-img" />
      </div>
      <MannequinChat m={mannequin} />
      <ul className="pieces">
        {items.map((item, i) => (
          <li key={item?.id ?? `gone-${i}`}>
            {item ? <ItemPic item={item} /> : <span className="pic" />}
            <span>
              {item?.name ?? GONE}
              {item && item.status !== "available" && <em className="pill" style={{ marginLeft: 6 }}>{item.status}</em>}
            </span>
            {canSwap && item && (
              <button type="button" className="swap" aria-label={`Swap ${item.name}`} title="Swap" disabled={busy} onClick={() => swap.mutate(item)}>
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M4 9h13l-3-3M20 15H7l3 3" />
                </svg>
              </button>
            )}
          </li>
        ))}
      </ul>
      <p className="why">
        {reason}
        {notes.map((n) => ` ${n}`)}
        {weather?.summary.source === "typical" && " Typical weather for this week, not a forecast."}
      </p>
      {suggestion.swap_item && <p className="hint">Try swapping in {suggestion.swap_item.name}.</p>}
      {swap.error && (
        <p role="alert" className="err">
          {swapErrorText(swap.error)}
        </p>
      )}
      {wear.error && (
        <p role="alert" className="err">
          {chatErrorText(wear.error)}
        </p>
      )}
      {!suggestion.wearable && <p className="hint">Some items are gone or not available, so this look cannot be worn now.</p>}
      {suggestion.wearable && !event && <p className="hint">Name the occasion first, then you can log this as worn.</p>}
      {saved ? (
        <div className="done" role="status">
          {dismissed ? (
            <p>Saved.</p>
          ) : laundry.isSuccess ? (
            <p>Saved, and marked as laundry.</p>
          ) : (
            <>
              <p>Saved as worn. Mark these as laundry?</p>
              <div className="row">
                <button className="btn primary sm" disabled={laundry.isPending} onClick={() => laundry.mutate()}>
                  {laundry.isPending ? "Marking…" : "Mark as laundry"}
                </button>
                <button className="btn sm" onClick={() => setDismissed(true)}>
                  Done
                </button>
              </div>
              {laundry.error && <p className="err">{errorMessage(laundry.error)}</p>}
            </>
          )}
        </div>
      ) : (
        <div className="look-foot">
          <button className="btn primary" aria-label={wear.isPending ? undefined : `Wear this: ${names}`} disabled={!canWear || busy} onClick={() => wear.mutate()}>
            {wear.isPending ? "Saving…" : "Wear this"}
          </button>
          <Thumbs key={itemIds.join("-")} itemIds={itemIds} rating={suggestion.rating} disabled={anyGone} />
        </div>
      )}
    </article>
  );
}
