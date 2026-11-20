import type { Event, Item } from "@/api/client";
import { ItemPic } from "@/components/wardrobe/GarmentIcons";
import { slotOf, type Slot } from "@/lib/wardrobe";

export const NOTE_TEXT: Record<string, string> = {
  no_footwear: "You have no footwear available, so these outfits have none.",
  footwear_not_rain_safe: "Rain likely, but none of your footwear is rain-safe.",
  footwear_mismatch: "None of your footwear suits this look, so the nearest pair stands in.",
  no_blouse: "Your saree or lehenga has no blouse to go with it, so it is left out. Add its blouse to the wardrobe.",
};
export const WEAR_REASON_MAX = 1000; // WearIn.reason limit

const pad = (n: number) => String(n).padStart(2, "0");

/** The local calendar date as YYYY-MM-DD (not UTC, so late evenings do not roll over). */
export function localToday(now = new Date()): string {
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/** Wear date: the event's own day when it is today or past, otherwise today (the server refuses future dates, AC-53a). */
export function wornOn(event: Event, today: string): string {
  const day = event.starts_at.slice(0, 10);
  return day <= today ? day : today;
}

/** The outfit laid out flat: each item in its slot; a just-swapped item drops in. */
export function Collage({ items, fresh }: { items: (Item | null)[]; fresh?: number | null }) {
  const taken = new Set<Slot>();
  return (
    <div className="lay">
      {items.map((item) => {
        if (!item) return null;
        const slot = slotOf(item, taken);
        taken.add(slot);
        return (
          <div key={item.id} className={`s-${slot}${fresh === item.id ? " new" : ""}`}>
            <ItemPic item={item} />
          </div>
        );
      })}
    </div>
  );
}
