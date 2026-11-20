export const CATEGORIES = ["top", "bottom", "dress", "outerwear", "footwear", "ethnic", "accessory", "bag"] as const;
export const PATTERNS = ["solid", "stripe", "check", "print", "floral", "embroidered", "other"] as const;
export const SEASONS = ["summer", "monsoon", "autumn", "winter", "spring"] as const;
export const STATUSES = ["available", "laundry", "tailor", "packed", "retired"] as const;

export type Category = (typeof CATEGORIES)[number];
export type Pattern = (typeof PATTERNS)[number];
export type Season = (typeof SEASONS)[number];
export type Status = (typeof STATUSES)[number];
export type Color = { hex: string; name: string; role: "primary" | "secondary" };
export type Tags = {
  name: string;
  category: Category;
  subcategory: string;
  colors: Color[];
  pattern: Pattern;
  fabric: string | null;
  formality: number;
  warmth: number;
  seasons: Season[];
  fit: string | null;
  tags: string[];
};

const TAG_KEYS = ["name", "category", "subcategory", "colors", "pattern", "fabric", "formality", "warmth", "seasons", "fit", "tags"] as const;

/** Item/proposed_tags rows come back loosely typed from JSON columns; the backend validated them. */
export function pickTags(src: object): Tags {
  const rec = src as Record<string, unknown>;
  return Object.fromEntries(TAG_KEYS.map((k) => [k, rec[k]])) as Tags;
}

export function changedFromAi(tags: Tags, ai: object | null | undefined, key: keyof Tags): boolean {
  return !!ai && JSON.stringify(tags[key]) !== JSON.stringify((ai as Record<string, unknown>)[key]);
}
