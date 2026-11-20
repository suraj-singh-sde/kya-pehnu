import type { Item } from "@/api/client";
import type { Category } from "@/lib/tags";

/** The almirah's compartments: rails hang, shelves stack, drawers and lockers have a front. `cats` lists the categories it holds. */
export type Kind = "rail" | "shelf" | "drawer" | "locker";
export type Section = { id: string; name: string; kind: Kind; row: number; col: number; icon: string; cats: Category[]; match?: Match };
export type Layout = { sections: Section[]; rows: number[] };
export type Gender = "" | "male" | "female";

const sec = (id: Category, name: string, kind: Kind, row: number, col: number, icon: string, cats: Category[] = [id]): Section => ({ id, name, kind, row, col, icon, cats });

/** Everyone's almirah: every category has a compartment. */
const ALL: Layout = {
  sections: [
    sec("bag", "Bags", "shelf", 0, -1, "tote"),
    sec("top", "Tops", "rail", 1, 0, "tee"),
    sec("outerwear", "Outerwear", "rail", 1, 1, "jacket"),
    sec("ethnic", "Ethnic", "rail", 2, 0, "kurta"),
    sec("dress", "Dresses", "rail", 2, 1, "dress"),
    sec("bottom", "Bottoms", "drawer", 3, 0, "trousers"),
    sec("accessory", "Accessories", "locker", 3, 1, "watch"),
    sec("footwear", "Footwear", "shelf", 4, -1, "sneaker"),
  ],
  rows: [1.15, 2, 2.3, 1.45, 1.2],
};
/** Word lists a compartment can narrow its categories to, by an item's name or subcategory; the clean-picture
 * prompt knows the same words. A saved shelf stores the list's key. */
export type Match = keyof typeof MATCHES;
const MATCHES = {
  caps_glasses: /\b(caps?|hats?|beanies?|snapbacks?|baseball|sunglass(es)?|shades|glasses|eyewear|spectacles|aviators?|wayfarers?)\b/i,
  // kept folded on a shelf, not hung: the clean-picture prompt folds the same words. A "saree blouse" hangs with the kurtas.
  sarees: /^(?![\s\S]*\b(?:blouses?|cholis?)\b)[\s\S]*\b(sarees?|saris?|dupattas?|chunnis?|odhnis?|stoles?|shawls?)\b/i,
  // a bracelet watch is a watch (the outfit rules read it the same way), so a watch never lands here
  jewellery:
    /^(?![\s\S]*\b(?:watch|smartwatch)\b)[\s\S]*\b(earrings?|jhumkas?|jhumkis?|studs?|hoops?|chandbalis?|danglers?|necklaces?|chokers?|mangalsutras?|haars?|pendants?|chains?|neckpieces?|rings?|bangles?|bracelets?|kadas?|cuffs?|anklets?|payals?|tikkas?|nose\s?(?:pins?|rings?)|naths?|brooch(?:es)?|jewell?ery)\b/i,
};
/** What a matched shelf is called, which category it borrows from, and its label's icon. */
export const MATCH_LABEL: Record<Match, string> = { caps_glasses: "Caps & glasses", sarees: "Sarees & dupattas", jewellery: "Jewellery" };
export const MATCH_CATS: Record<Match, Category> = { caps_glasses: "accessory", sarees: "ethnic", jewellery: "accessory" };
const MATCH_ICON: Record<Match, string> = { caps_glasses: "cap", sarees: "saree", jewellery: "jhumka" };
/** A man's almirah: no dress rail or bag shelf; caps and sunglasses get the top shelf, kurtas and sherwanis a
 * full-width rail (a dress, if any, hangs there too), and a bag, if any, goes in the locker. */
const MALE: Layout = {
  sections: [
    { ...sec("accessory", "Caps & glasses", "shelf", 0, -1, "cap"), id: "caps", match: "caps_glasses" },
    sec("top", "Shirts & tees", "rail", 1, 0, "tee"),
    sec("outerwear", "Jackets", "rail", 1, 1, "jacket"),
    sec("ethnic", "Kurtas & sherwanis", "rail", 2, -1, "kurta", ["ethnic", "dress"]),
    sec("bottom", "Trousers", "drawer", 3, 0, "trousers"),
    sec("accessory", "Watches & belts", "locker", 3, 1, "watch", ["accessory", "bag"]),
    sec("footwear", "Shoes", "shelf", 4, -1, "sneaker"),
  ],
  rows: [1, 2.2, 2.2, 1.45, 1.2],
};
/** A woman's almirah: dresses and kurtas get the tall rails, tops and jackets share one; sarees and dupattas lie
 * folded on a shelf, jewellery has a locker, and bags share the top shelf with the other extras (belts, watches). */
const FEMALE: Layout = {
  sections: [
    sec("bag", "Bags & extras", "shelf", 0, -1, "tote", ["bag", "accessory"]),
    sec("dress", "Dresses", "rail", 1, 0, "dress"),
    sec("ethnic", "Kurtas & suits", "rail", 1, 1, "kurta"),
    sec("top", "Tops & jackets", "rail", 2, 0, "tee", ["top", "outerwear"]),
    { ...sec("ethnic", "Sarees & dupattas", "shelf", 2, 1, "saree"), id: "sarees", match: "sarees" },
    sec("bottom", "Bottoms & skirts", "drawer", 3, 0, "trousers"),
    { ...sec("accessory", "Jewellery", "locker", 3, 1, "jhumka"), id: "jewellery", match: "jewellery" },
    sec("footwear", "Footwear", "shelf", 4, -1, "flat"),
  ],
  rows: [1.15, 2.4, 1.9, 1.45, 1.2],
};
export const layoutFor = (gender: Gender | undefined): Layout => (gender === "male" ? MALE : gender === "female" ? FEMALE : ALL);

/** Whether a compartment holds an item: its category is listed, and a compartment with `match` takes only the items
 * whose name or subcategory it matches, leaving the rest of that category to the layout's other compartments. */
export const holds = (layout: Layout, s: Section, item: Item): boolean => {
  const cat = item.category as Category;
  if (!s.cats.includes(cat)) return false;
  const words = `${item.name} ${item.subcategory ?? ""}`;
  if (s.match) return MATCHES[s.match].test(words);
  return !layout.sections.some((o) => o.match && o.cats.includes(cat) && MATCHES[o.match].test(words));
};

/** A shelf as the person edits it on the Account page (and as the profile stores it). */
export type Shelf = { name: string; kind: Kind; width: "full" | "half"; cats: Category[]; match?: Match };
export const CATEGORY_LABEL: Record<Category, string> = {
  top: "Tops",
  bottom: "Bottoms",
  dress: "Dresses",
  outerwear: "Outerwear",
  footwear: "Footwear",
  ethnic: "Ethnic wear",
  accessory: "Accessories",
  bag: "Bags",
};
const ICON_FOR: Record<Category, string> = { top: "tee", bottom: "trousers", dress: "dress", outerwear: "jacket", footwear: "sneaker", ethnic: "kurta", accessory: "watch", bag: "tote" };
const ROW_WEIGHT: Record<Kind, number> = { rail: 2.2, shelf: 1.1, drawer: 1.45, locker: 1.45 };
export const MAX_SHELVES = 12;

/** The gender's default almirah as a shelf list: the editor's starting point and what "Reset" goes back to. */
export const defaultShelves = (gender: Gender | undefined): Shelf[] =>
  layoutFor(gender).sections.map((s) => ({
    name: s.name,
    kind: s.kind,
    width: s.col < 0 ? "full" : "half",
    cats: [...s.cats],
    ...(s.match && { match: s.match }),
  }));

/** Each category on one shelf only, the first that lists it: a layout saved while a category could sit on two shelves
 * showed its pieces on both (T-shirts on the shoe shelf). */
export const oneShelfEach = (shelves: Shelf[]): Shelf[] => {
  const seen = new Set<Category>();
  // A matched shelf (caps) only borrows some pieces of its categories, so it neither claims nor loses them.
  return shelves.map((s) => (s.match ? s : { ...s, cats: s.cats.filter((c) => !seen.has(c) && !!seen.add(c)) }));
};

/** The almirah from a shelf list: full shelves take a row, two halves share one, and whatever no shelf holds lands on
 * an "Everything else" shelf at the bottom so no piece is ever hidden. */
export function layoutFromShelves(saved: Shelf[]): Layout {
  const shelves = oneShelfEach(saved); // same count and order: arrange mode maps compartments to shelves by index
  const held = new Set(shelves.flatMap((s) => (s.match ? [] : s.cats)));
  const rest = (Object.keys(CATEGORY_LABEL) as Category[]).filter((c) => !held.has(c));
  const all: Shelf[] = rest.length ? [...shelves, { name: "Everything else", kind: "shelf", width: "full", cats: rest }] : shelves;
  const sections: Section[] = [];
  const rows: number[] = [];
  let row = -1;
  let halfOpen = false; // the previous shelf was a half with its right side still free
  all.forEach((sh, i) => {
    const id = `s${i}`;
    const icon = sh.match ? MATCH_ICON[sh.match] : (ICON_FOR[sh.cats[0]] ?? "tote");
    const match = sh.match && { match: sh.match };
    const nextIsHalf = all[i + 1]?.width === "half";
    if (sh.width === "half" && halfOpen) {
      sections.push({ id, name: sh.name, kind: sh.kind, row, col: 1, icon, cats: sh.cats, ...match });
      rows[row] = Math.max(rows[row], ROW_WEIGHT[sh.kind]);
      halfOpen = false;
    } else if (sh.width === "half" && nextIsHalf) {
      row += 1;
      sections.push({ id, name: sh.name, kind: sh.kind, row, col: 0, icon, cats: sh.cats, ...match });
      rows.push(ROW_WEIGHT[sh.kind]);
      halfOpen = true;
    } else {
      row += 1; // a full shelf, or a half with nobody to pair with
      sections.push({ id, name: sh.name, kind: sh.kind, row, col: -1, icon, cats: sh.cats, ...match });
      rows.push(ROW_WEIGHT[sh.kind]);
      halfOpen = false;
    }
  });
  return { sections, rows };
}

/** The cabinet's finishes: a name and the colour its swatch shows. The CSS (`[data-finish]`) carries the rest. */
export const FINISHES = [
  { id: "teal", label: "Teal steel", swatch: "#4f6f68" },
  { id: "walnut", label: "Walnut", swatch: "#6b4a2f" },
  { id: "oak", label: "Oak", swatch: "#b8905c" },
  { id: "ivory", label: "Ivory", swatch: "#e9e4d8" },
  { id: "navy", label: "Navy", swatch: "#2f4160" },
  { id: "rose", label: "Rose", swatch: "#b76e79" },
] as const;
export type Finish = (typeof FINISHES)[number]["id"];

export const SECTIONS = ALL.sections;
export const section = (id: string): Section => SECTIONS.find((s) => s.id === id) ?? SECTIONS[1];

export const STATUS_TEXT: Record<string, string> = { laundry: "In the laundry", tailor: "At the tailor", packed: "Packed" };
export const FORMALITY = ["", "Lounge", "Casual", "Smart casual", "Dressy", "Formal"];
export const WARMTH = ["", "Very light", "Light", "Medium", "Warm", "Very warm"];

/** The primary colour of an item, or its first, as `{hex, name}`; items come back with loosely typed colour rows. */
export function mainColor(item: Item): { hex: string; name: string } | null {
  const rows = (item.colors ?? []) as { hex?: string; name?: string; role?: string }[];
  const c = rows.find((r) => r.role === "primary") ?? rows[0];
  return c?.hex ? { hex: c.hex, name: c.name ?? "" } : null;
}

/** Where an item sits in the outfit collage. */
export type Slot = "main" | "bottom" | "feet" | "bag" | "extra" | "more" | "more2" | "more3" | "more4";
const SPARE: Slot[] = ["more", "more2", "more3", "more4"];
export function slotOf(item: Item, taken: Set<Slot>): Slot {
  const want: Slot =
    item.category === "bottom" ? "bottom"
    : item.category === "footwear" ? "feet"
    : item.category === "bag" ? "bag"
    : item.category === "accessory" ? "extra"
    : "main";
  // the next accessories (a watch, earrings, a necklace, bangles) take the spare spots in turn
  if (!taken.has(want)) return want;
  return SPARE.find((s) => !taken.has(s)) ?? "more4";
}
