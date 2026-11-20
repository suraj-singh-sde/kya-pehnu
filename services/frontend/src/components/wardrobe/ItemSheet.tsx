import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { Link } from "react-router-dom";
import { api, errorMessage, unwrap, type Item } from "@/api/client";
import { ItemPic } from "@/components/wardrobe/GarmentIcons";
import { FORMALITY, STATUS_TEXT, WARMTH, mainColor, section } from "@/lib/wardrobe";
import AddItems from "@/pages/AddItems";

export type SheetTarget = "basket" | "add" | number | null;
type Props = { target: SheetTarget; items: Item[]; onClose: () => void };

const REFRESH = ["items", "item"];

function Meter({ n }: { n: number }) {
  return (
    <span className="meter" role="img" aria-label={`${n} of 5`}>
      {[1, 2, 3, 4, 5].map((i) => (
        <i key={i} className={i <= n ? "on" : undefined} />
      ))}
    </span>
  );
}

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

/** "14 times, last 3 days ago", from the detail's wear log (newest first). */
function wornText(wears: { worn_on: string }[]): string {
  if (!wears.length) return "Not worn yet";
  const last = wears.reduce((a, b) => (a > b.worn_on ? a : b.worn_on), "");
  const days = Math.round((Date.now() - new Date(last).getTime()) / 86_400_000);
  const when = days <= 0 ? "today" : days === 1 ? "yesterday" : `${days} days ago`;
  return `${wears.length} ${wears.length === 1 ? "time" : "times"}, last ${when}`;
}

/** The item or laundry dialog over the almirah. Status changes and wears go to the API; the list refetches after. */
export function ItemSheet({ target, items, onClose }: Props) {
  const qc = useQueryClient();
  const on = target !== null;
  const close = useRef<HTMLButtonElement>(null);
  const returnTo = useRef<Element | null>(null);
  useEffect(() => {
    if (on) {
      returnTo.current = document.activeElement;
      close.current?.focus({ preventScroll: true });
    } else if (returnTo.current instanceof HTMLElement && returnTo.current.isConnected) returnTo.current.focus({ preventScroll: true });
  }, [on]);
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && on && onClose();
    addEventListener("keydown", k);
    return () => removeEventListener("keydown", k);
  }, [on, onClose]);

  const refresh = () => Promise.all(REFRESH.map((k) => qc.invalidateQueries({ queryKey: [k] })));
  const status = useMutation({
    mutationFn: (v: { ids: number[]; status: "available" | "laundry" }) => unwrap(api.POST("/api/items/status", { body: { item_ids: v.ids, status: v.status } })),
    onSuccess: (_items, v) => {
      void refresh();
      if (v.status === "laundry") onClose(); // the piece is in the basket now
    },
  });
  const worn = useMutation({
    mutationFn: (id: number) => unwrap(api.POST("/api/items/{item_id}/worn-today", { params: { path: { item_id: id } } })),
    onSuccess: refresh,
  });
  const busy = status.isPending || worn.isPending;
  const error = status.error ?? worn.error;

  const item = typeof target === "number" ? items.find((i) => i.id === target) : undefined;
  const laundry = items.filter((i) => i.status === "laundry");
  const detail = useQuery({
    queryKey: ["item", item?.id],
    queryFn: () => unwrap(api.GET("/api/items/{item_id}", { params: { path: { item_id: item!.id! } } })),
    enabled: !!item,
  });

  return (
    <>
      <div className={`scrim${on ? " on" : ""}`} onClick={onClose} />
      <aside className={`sheet${on ? " on" : ""}${target === "add" ? " wide" : ""}`} role="dialog" aria-modal="true" aria-label={item ? item.name : target === "add" ? "Add items" : "Laundry"}>
        <button ref={close} className="x" aria-label="Close" onClick={onClose}>
          ×
        </button>
        {item && (
          <ItemView item={item} wears={detail.data?.wears} busy={busy} onWorn={() => worn.mutate(item.id!)} onStatus={(s) => status.mutate({ ids: [item.id!], status: s })} />
        )}
        {target === "add" && <AddItems onFinished={onClose} />}
        {target === "basket" && (
          <>
            <h2>Laundry</h2>
            <p className="sub">{laundry.length ? "Pieces here are left out of outfit suggestions." : "The basket is empty."}</p>
            <div style={{ margin: "16px 0" }}>
              {laundry.map((it) => (
                <div key={it.id} className="lrow">
                  <ItemPic item={it} />
                  <span>{it.name}</span>
                  <button className="btn sm" disabled={busy} onClick={() => status.mutate({ ids: [it.id!], status: "available" })}>
                    Mark clean
                  </button>
                </div>
              ))}
            </div>
            {laundry.length > 1 && (
              <button className="btn primary" disabled={busy} onClick={() => status.mutate({ ids: laundry.map((i) => i.id!), status: "available" })}>
                Mark all clean
              </button>
            )}
          </>
        )}
        {error && (
          <p role="alert" className="err" style={{ marginTop: 10 }}>
            {errorMessage(error)}
          </p>
        )}
        <div role="status" className="okline" style={{ marginTop: 10 }}>
          {worn.isSuccess && item && "Logged as worn today"}
        </div>
      </aside>
    </>
  );
}

type ViewProps = { item: Item; wears?: { worn_on: string }[]; busy: boolean; onWorn: () => void; onStatus: (s: "available" | "laundry") => void };

function ItemView({ item, wears, busy, onWorn, onStatus }: ViewProps) {
  const color = mainColor(item);
  const fact = (k: string, v: React.ReactNode) => (
    <div>
      <dt>{k}</dt>
      <dd>{v}</dd>
    </div>
  );
  return (
    <>
      <div className="hero">
        <ItemPic item={item} />
      </div>
      <h2>{item.name}</h2>
      <p className="sub">
        {item.subcategory}, in {section(item.category).name} {STATUS_TEXT[item.status] && <em className="pill">{STATUS_TEXT[item.status]}</em>}
      </p>
      <dl className="facts">
        {fact("Color", color ? <><i className="sw" style={{ background: color.hex }} />{color.name}</> : "—")}
        {fact("Fabric", item.fabric || "—")}
        {fact("Pattern", cap(item.pattern || "solid"))}
        {fact("Formality", <><Meter n={item.formality} />{FORMALITY[item.formality]}</>)}
        {fact("Warmth", <><Meter n={item.warmth} />{WARMTH[item.warmth]}</>)}
        {fact("Seasons", item.seasons?.length ? item.seasons.map((s) => <span key={s} className="tag">{cap(s)}</span>) : "All year")}
        {fact("Worn", wears ? wornText(wears) : "…")}
      </dl>
      <div className="row">
        {item.status === "available" ? (
          <>
            <button className="btn primary" disabled={busy} onClick={onWorn}>Worn today</button>
            <button className="btn" disabled={busy} onClick={() => onStatus("laundry")}>Send to laundry</button>
          </>
        ) : (
          <button className="btn primary" disabled={busy} onClick={() => onStatus("available")}>
            {item.status === "laundry" ? "Mark clean" : "Back in the wardrobe"}
          </button>
        )}
        <Link className="btn ghost" to={`/items/${item.id}`}>Edit details</Link>
      </div>
    </>
  );
}
