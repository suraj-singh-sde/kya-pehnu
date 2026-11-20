import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { api, errorMessage, unwrap } from "@/api/client";
import { Almirah } from "@/components/wardrobe/Almirah";
import { Lamp, PhotoFrame } from "@/components/wardrobe/Furniture";
import { ItemSheet, type SheetTarget } from "@/components/wardrobe/ItemSheet";
import { Clothesline, WardrobeHeader } from "@/components/wardrobe/WardrobeHeader";
import {
  CATEGORY_LABEL,
  FINISHES,
  defaultShelves,
  layoutFor,
  layoutFromShelves,
  oneShelfEach,
  type Finish,
  type Shelf,
} from "@/lib/wardrobe";

/** The wardrobe as an almirah on a lit wall. `location.state.open` (from Add items) opens one compartment. */
export default function Wardrobe() {
  const qc = useQueryClient();
  const items = useQuery({
    queryKey: ["items", "all"],
    queryFn: () => unwrap(api.GET("/api/items")),
  });
  const profile = useQuery({
    queryKey: ["profile"],
    queryFn: () => unwrap(api.GET("/api/profile")),
  });
  const custom = profile.data?.layout as
    { shelves?: Shelf[] } | null | undefined;
  const jump = (useLocation().state as { open?: string } | null)?.open ?? null;
  // On a phone the almirah keeps its size: a compartment hangs its pieces on the clothesline above it, not in place.
  const phone = () => matchMedia("(max-width: 760px)").matches;
  const [doors, setDoors] = useState(!!jump); // closed until clicked
  const [open, setOpen] = useState<string | null>(phone() ? null : jump);
  const [shelf, setShelf] = useState<string | null>(phone() ? jump : null);
  const [sheet, setSheet] = useState<SheetTarget>(null);

  // Arrange mode: a draft of the shelves and a finish preview, saved together on Done, dropped on Cancel.
  const [draft, setDraft] = useState<Shelf[] | null>(null);
  const [preview, setPreview] = useState<Finish | null>(null);
  const theme = preview ?? profile.data?.theme ?? "teal";
  const layout = draft
    ? layoutFromShelves(draft)
    : custom?.shelves?.length
      ? layoutFromShelves(custom.shelves)
      : layoutFor(profile.data?.gender);
  const startArrange = () => {
    setOpen(null);
    setDraft(
      custom?.shelves?.length
        ? oneShelfEach(custom.shelves).filter((s) => s.cats.length > 0)
        : defaultShelves(profile.data?.gender),
    );
  };
  const cancelArrange = () => {
    setDraft(null);
    setPreview(null);
    save.reset();
  };
  const save = useMutation({
    mutationFn: (v: { shelves: Shelf[]; theme: Finish }) => {
      const { id: _id, ...base } = profile.data!;
      return unwrap(
        api.PUT("/api/profile", {
          body: { ...base, layout: { shelves: v.shelves }, theme: v.theme },
        }),
      );
    },
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ["profile"] });
      cancelArrange();
    },
  });
  const problem = draft?.find((s) => !s.name.trim() || s.cats.length === 0);
  const unheld = draft
    ? (Object.keys(CATEGORY_LABEL) as (keyof typeof CATEGORY_LABEL)[]).filter(
        (c) => !draft.some((s) => !s.match && s.cats.includes(c)),
      )
    : [];

  // Escape: close the sheet, then arrange mode, then the compartment, then the doors. The sheet handles its own.
  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || sheet !== null) return;
      if (draft) cancelArrange();
      else if (open) setOpen(null);
      else if (doors) setDoors(false);
    };
    addEventListener("keydown", k);
    return () => removeEventListener("keydown", k);
  }, [sheet, open, doors, draft]); // eslint-disable-line react-hooks/exhaustive-deps

  const all = items.data ?? [];
  return (
    <div aria-label="Wardrobe">
      {items.error && <p className="err">{errorMessage(items.error)}</p>}
      <WardrobeHeader
        items={all}
        onAdd={() => setSheet("add")}
        onLaundry={() => setSheet("basket")}
        onArrange={doors && !draft && profile.data ? startArrange : undefined}
      />
      <div className="mob" inert={sheet !== null}>
        <Clothesline
          shelf={layout.sections.find((s) => s.id === shelf) ?? null}
          items={all}
          doors={doors}
          selected={typeof sheet === "number" ? sheet : null}
          onPick={(it) => setSheet(it.id!)}
          onClear={() => setShelf(null)}
        />
      </div>
      <div className={`arrbar desk${draft ? " on" : ""}`} inert={!draft}>
        {draft && (
          <>
            <span className="ttl">Arrange your almirah</span>
            <div className="chips" role="group" aria-label="Almirah finish">
              {FINISHES.map((f) => (
                <button
                  key={f.id}
                  type="button"
                  className="fin"
                  title={f.label}
                  aria-label={f.label}
                  aria-pressed={theme === f.id}
                  style={{ background: f.swatch }}
                  onClick={() => setPreview(f.id)}
                />
              ))}
            </div>
            <span className="note" role="status">
              {problem
                ? "Every shelf needs a name and at least one category."
                : save.error
                  ? errorMessage(save.error)
                  : unheld.length
                    ? `Not on any shelf, so they go on “Everything else”: ${unheld.map((c) => CATEGORY_LABEL[c]).join(", ")}.`
                    : ""}
            </span>
            <button
              type="button"
              className="btn sm ghost"
              onClick={cancelArrange}
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn sm primary"
              disabled={!!problem || save.isPending}
              onClick={() => save.mutate({ shelves: draft, theme })}
            >
              Done
            </button>
          </>
        )}
      </div>
      <div className="stage" data-finish={theme} inert={sheet !== null}>
        <PhotoFrame />
        <Almirah
          items={all}
          layout={layout}
          doorsOpen={doors}
          open={draft ? null : open}
          hung={shelf}
          selected={typeof sheet === "number" ? sheet : null}
          arrange={draft ? { shelves: draft, onChange: setDraft } : null}
          onDoors={(v) => {
            if (!v) {
              cancelArrange();
              setShelf(null);
            }
            setDoors(v);
          }}
          onOpen={(id) => {
            if (id && phone()) setShelf(id);
            else setOpen(id);
          }}
          onPick={(it) => setSheet(it.id!)}
        />
        <Lamp />
      </div>
      <ItemSheet target={sheet} items={all} onClose={() => setSheet(null)} />
    </div>
  );
}
