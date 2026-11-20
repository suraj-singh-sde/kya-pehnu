import { imageUrl, type Item } from "@/api/client";

/** Silhouettes for the compartment labels. Real items show their cut-out photo (see ItemPic). */
const SYMBOLS: Record<string, { h: number; d: string }> = {
  tee: { h: 130, d: "M40 16Q50 25 60 16L82 24 96 48 84 56 78 45V112H22V45L16 56 4 48 18 24Z" },
  jacket: { h: 130, d: "M40 16H60L82 24 97 88 86 91 78 50V114H22V50L14 91 3 88 18 24Z" },
  kurta: { h: 130, d: "M40 16Q50 27 60 16L80 24 95 82 85 85 77 52 80 126H20L23 52 15 85 5 82 20 24Z" },
  dress: { h: 130, d: "M37 16H44Q50 27 56 16H63L66 42Q62 52 64 60L86 124H14L36 60Q38 52 34 42Z" },
  trousers: { h: 130, d: "M28 16H72L79 124H56L50 54 44 124H21Z" },
  sneaker: { h: 60, d: "M6 44Q6 24 18 20H42Q54 37 80 39 95 41 95 50V55H6Z" },
  tote: { h: 110, d: "M16 44H84L90 104H10ZM32 44Q32 14 50 14 68 14 68 44" },
  watch: { h: 100, d: "M40 6h20v88H40Z M50 31a19 19 0 1 0 0 38 19 19 0 1 0 0-38Z" },
  jhumka: { h: 100, d: "M8 62Q8 34 22 34T36 62ZM64 62Q64 34 78 34T92 62ZM17 72a5 5 0 1 0 10 0 5 5 0 1 0-10 0ZM73 72a5 5 0 1 0 10 0 5 5 0 1 0-10 0Z" },
  cap: { h: 60, d: "M10 46Q10 12 44 12 74 12 78 40L95 44Q99 52 90 52H10Z" },
  flat: { h: 60, d: "M5 47Q28 30 52 39 76 46 95 41 98 54 90 55H10Q3 55 5 47Z" },
  // a stack of three folded sarees, the top one's pallu turned back
  saree: { h: 70, d: "M8 52H92V62H8ZM10 36H90V48H10ZM12 14H88V32H12ZM12 14L50 32 88 14Z" },
};

export function SectionIcon({ name }: { name: string }) {
  const s = SYMBOLS[name] ?? SYMBOLS.tee;
  return (
    <svg className="ic" viewBox={`0 0 100 ${s.h}`} aria-hidden="true">
      <path d={s.d} />
    </svg>
  );
}

/** An item's picture; items out of the wardrobe (laundry, tailor, packed) are faded. */
export function ItemPic({ item, className = "" }: { item: Item; className?: string }) {
  const ghost = item.status !== "available";
  return <img className={`pic${ghost ? " ghost" : ""} ${className}`} src={imageUrl(item.thumb_path)} alt="" loading="lazy" draggable={false} />;
}
