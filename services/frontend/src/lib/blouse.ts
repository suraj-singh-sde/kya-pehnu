import type { Item, Piece } from "@/api/client";

/** The words the outfit rules read the same way (`slots.needs_blouse` and the blouse slot). */
const NEEDS_BLOUSE = /\b(sarees?|saris?|lehengas?|lehngas?|ghagras?)\b/i;
const BLOUSE = /\b(blouses?|cholis?)\b/i;

type Tagged = { category?: string | null; subcategory?: string | null; name?: string | null };
const words = (t: Tagged) => `${t.subcategory ?? ""} ${t.name ?? ""}`;
const isBlouse = (t: Tagged | null | undefined) => !!t && t.category === "ethnic" && BLOUSE.test(words(t));

/** A line asking for the blouse of a saree (or the choli of a lehenga) in the review queue, when no blouse is in the
 * queue or the wardrobe yet; null otherwise. The outfit rules leave a saree out until it has one. */
export function blouseHint(piece: Piece, pieces: Piece[], items: Item[]): string | null {
  const tags = piece.proposed_tags as Tagged | null | undefined;
  if (!tags || tags.category !== "ethnic" || isBlouse(tags) || !NEEDS_BLOUSE.test(words(tags))) return null;
  const haveOne = pieces.some((p) => p.id !== piece.id && p.state === "pending_review" && isBlouse(p.proposed_tags as Tagged)) || items.some(isBlouse);
  if (haveOne) return null;
  const lehenga = /\b(lehengas?|lehngas?|ghagras?)\b/i.test(words(tags));
  return lehenga ? "A lehenga needs its choli. Add a photo of the choli too, so the stylist can pair them." : "A saree needs its blouse. Add a photo of the blouse too, so the stylist can pair them.";
}
