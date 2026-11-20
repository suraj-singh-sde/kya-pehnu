import { useEffect, useRef, type KeyboardEvent } from "react";
import type { Item } from "@/api/client";
import { ItemPic } from "@/components/wardrobe/GarmentIcons";
import type { Section } from "@/lib/wardrobe";

type Props = { items: Item[]; onAdd: () => void; onLaundry: () => void; onArrange?: () => void };

/** The bar above the almirah: the piece count, Arrange almirah (desktop, while the doors are open), laundry and add. */
export function WardrobeHeader({ items, onAdd, onLaundry, onArrange }: Props) {
  const laundry = items.filter((i) => i.status === "laundry").length;
  return (
    <header className="mw-head">
      <div>
        <h1>Wardrobe</h1>
        <p className="sub">
          {items.length} {items.length === 1 ? "piece" : "pieces"}
          {laundry > 0 && `, ${laundry} in the laundry`}
        </p>
      </div>
      {onArrange && (
        <button type="button" className="btn sm ghost desk" onClick={onArrange}>
          Arrange almirah
        </button>
      )}
      <button type="button" className="mw-icon" aria-label={`Laundry, ${laundry} pieces`} onClick={onLaundry}>
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M4 9h16l-1.6 10.2a2 2 0 0 1-2 1.8H7.6a2 2 0 0 1-2-1.8ZM8 9l2-5M16 9l-2-5" />
        </svg>
        {laundry > 0 && <span className="mw-dot">{laundry}</span>}
      </button>
      <button type="button" className="mw-icon add" aria-label="Add clothes" onClick={onAdd}>
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M12 5v14M5 12h14" />
        </svg>
      </button>
    </header>
  );
}

type LineProps = {
  shelf: Section | null;
  items: Item[];
  doors: boolean;
  selected?: number | null;
  onPick: (item: Item) => void;
  onClear: () => void;
};

// The rail: a quadratic sag between two brackets, its ends at Y1 and its middle `dip` px lower. The dip grows with the
// rail's width and with the pieces hung on it. Hangers snap to stops --slot wide; the one at the centre turns to face
// front, grows and pushes its neighbours --push px aside, the rest show their side. The sizes come from the CSS, so the
// same rail hangs small above the phone's almirah and large inside an open compartment on desktop.
const Y1 = 10;
const DIP = 16; // the sag the path is drawn with; --sag scales it
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

/** A rail with a compartment's pieces on it as a carousel: swipe or drag it, tap a side piece to centre it, the centred one to open it. */
export function Clothesline({ shelf, items, doors, selected = null, onPick, onClear }: LineProps) {
  const hung = shelf ? items.filter((i) => shelf.cats.includes(i.category as Section["cats"][number])) : [];
  const line = useRef<HTMLElement>(null);
  const row = useRef<HTMLDivElement>(null);
  const dragged = useRef(false); // a mouse drag must not count as a click on the piece under it
  const swallow = useRef(0); // until when a click is the tail of a mouse tap already handled on pointer-up
  const latest = useRef({ hung, onPick });
  latest.current = { hung, onPick };

  // A tap on a side hanger brings it to the centre; a tap on the centred one opens its sheet.
  const tap = (p: HTMLElement) => {
    const el = row.current!;
    const it = latest.current.hung.find((i) => String(i.id) === p.dataset.id);
    if (!it) return;
    const slot = parseFloat(getComputedStyle(el).getPropertyValue("--slot")) || 56;
    const off = p.offsetLeft + p.offsetWidth / 2 - el.scrollLeft - el.clientWidth / 2;
    if (Math.abs(off) < slot / 2) latest.current.onPick(it);
    else el.scrollTo({ left: el.scrollLeft + off, behavior: "smooth" });
  };

  // Each frame the row scrolls, place every hanger from its distance to the centre (in stops): where it sits on the curve,
  // how far it turns, its size and stacking. The values go to CSS variables, so scrolling never re-renders.
  useEffect(() => {
    const el = row.current;
    const sec = line.current;
    if (!el || !sec) return;
    const css = (name: string, fallback: number) => parseFloat(getComputedStyle(el).getPropertyValue(name)) || fallback;
    let raf = 0;
    const place = () => {
      raf = 0;
      const w = el.clientWidth;
      const slot = css("--slot", 56);
      const push = css("--push", 34);
      const dipMax = clamp(w * 0.045, 16, 28);
      const dip = shelf ? Math.min(dipMax, dipMax / 2 + 1.6 * hung.length) : dipMax * 0.45;
      sec.style.setProperty("--sag", String(dip / DIP));
      sec.style.setProperty("--y0", `${Y1 + dip + 2}px`);
      const left = el.getBoundingClientRect().left;
      for (const p of el.querySelectorAll<HTMLElement>(".peg")) {
        const r = p.getBoundingClientRect();
        const t = (r.left + r.width / 2 - left - w / 2) / slot;
        const n = clamp(t, -1, 1);
        const dx = Math.sign(t) * push * Math.min(Math.abs(t), 1);
        const x = clamp((w / 2 + t * slot + dx) / w, 0, 1);
        p.style.setProperty("--dx", `${dx}px`);
        p.style.setProperty("--y", `${Y1 + 4 * dip * x * (1 - x)}px`);
        p.style.setProperty("--ry", `${n * 70}deg`);
        p.style.setProperty("--f", `${1 - Math.abs(n)}`);
        p.style.zIndex = String(Math.round((1 - Math.abs(n)) * 100));
      }
    };
    const onScroll = () => {
      if (!raf) raf = requestAnimationFrame(place);
    };
    // A mouse: its wheel scrolls the rail sideways, and the rail can be dragged.
    const wheel = (e: WheelEvent) => {
      if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
        e.preventDefault();
        el.scrollBy({ left: e.deltaY });
      }
    };
    let x0: number | null = null;
    let s0 = 0;
    const down = (e: PointerEvent) => {
      if (e.pointerType !== "mouse" || e.button !== 0) return;
      x0 = e.clientX;
      s0 = el.scrollLeft;
      dragged.current = false;
    };
    const move = (e: PointerEvent) => {
      if (x0 === null) return;
      const d = e.clientX - x0;
      if (!dragged.current && Math.abs(d) > 4) {
        // Only now is it a drag: capturing earlier would retarget a plain click away from the piece under it.
        dragged.current = true;
        el.classList.add("drag");
        el.setPointerCapture(e.pointerId);
      }
      if (dragged.current) el.scrollLeft = s0 - d;
    };
    const cancel = () => {
      x0 = null;
      el.classList.remove("drag"); // snap comes back and settles the nearest stop
    };
    // A mouse tap is handled here, on the piece under the pointer when it lifts: a click would go to the row instead
    // whenever the rail was still settling between press and release. The click that follows is then ignored.
    const up = (e: PointerEvent) => {
      if (x0 === null) return;
      cancel();
      if (dragged.current) return;
      const peg = (document.elementFromPoint(e.clientX, e.clientY) as HTMLElement | null)?.closest<HTMLElement>(".peg");
      if (peg && el.contains(peg)) {
        swallow.current = performance.now() + 400;
        tap(peg);
      }
    };
    place();
    el.addEventListener("scroll", onScroll, { passive: true });
    el.addEventListener("wheel", wheel, { passive: false });
    el.addEventListener("pointerdown", down);
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", up);
    el.addEventListener("pointercancel", cancel);
    addEventListener("resize", onScroll);
    return () => {
      el.removeEventListener("scroll", onScroll);
      el.removeEventListener("wheel", wheel);
      el.removeEventListener("pointerdown", down);
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerup", up);
      el.removeEventListener("pointercancel", cancel);
      removeEventListener("resize", onScroll);
      cancelAnimationFrame(raf);
    };
  }, [shelf, hung.length]); // eslint-disable-line react-hooks/exhaustive-deps

  // Touch and keyboard taps arrive as clicks; a mouse's was handled on pointer-up, and a drag's tail is not a tap.
  const click = (p: HTMLElement) => {
    if (performance.now() < swallow.current) return;
    if (dragged.current) {
      dragged.current = false;
      return;
    }
    tap(p);
  };
  const keys = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const el = row.current!;
    const slot = parseFloat(getComputedStyle(el).getPropertyValue("--slot")) || 56;
    el.scrollBy({ left: e.key === "ArrowRight" ? slot : -slot, behavior: "smooth" });
  };

  return (
    <section ref={line} className={`line${shelf ? " full" : ""}`} aria-label={shelf ? `${shelf.name} on the rail` : "Rail"}>
      <div className="line-cap">
        {shelf && (
          <>
            <b>{shelf.name}</b> {hung.length} {hung.length === 1 ? "piece" : "pieces"}
            <button type="button" className="lnk" onClick={onClear}>
              Take down
            </button>
          </>
        )}
      </div>
      <div className="line-area">
        <svg className="rope" viewBox="0 0 100 60" preserveAspectRatio="none" aria-hidden="true">
          <defs>
            <linearGradient id="rail" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stopColor="var(--rail-hi)" />
              <stop offset="1" stopColor="var(--rail-lo)" />
            </linearGradient>
          </defs>
          <path d={`M2 ${Y1}Q50 ${Y1 + 2 * DIP} 98 ${Y1}`} />
        </svg>
        {!shelf && (
          <>
            <div className="line-ghost" aria-hidden="true">
              {[0, 1, 2].map((i) => (
                <svg key={i} viewBox="0 0 40 26">
                  <path d="M23 3a3 3 0 1 0-3 3v4L3 21.5a1 1 0 0 0 .6 1.8h32.8a1 1 0 0 0 .6-1.8L20 10" />
                </svg>
              ))}
            </div>
            <p className="line-hint">{doors ? "Tap a shelf to hang its pieces here" : "Open the almirah and tap a shelf"}</p>
          </>
        )}
        <div className="line-row" ref={row} key={shelf?.id} onKeyDown={keys}>
          {hung.map((it, i) => (
            <button
              key={it.id}
              type="button"
              className={`peg${selected === it.id ? " on" : ""}`}
              aria-label={it.name}
              data-id={it.id}
              style={{ "--i": i } as React.CSSProperties}
              onClick={(e) => click(e.currentTarget)}
            >
              <span className="hang">
                <svg className="wire" viewBox="0 0 10 22" preserveAspectRatio="none" aria-hidden="true">
                  <path d="M5 1v21" />
                </svg>
                <span className="gar">
                  <ItemPic item={it} />
                </span>
                <span className="tagname">
                  <span>{it.name}</span>
                </span>
              </span>
            </button>
          ))}
          {shelf && !hung.length && <p className="line-empty">Nothing on this shelf yet</p>}
        </div>
      </div>
    </section>
  );
}
