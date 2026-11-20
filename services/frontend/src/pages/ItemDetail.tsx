import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, ApiError, errorMessage, imageUrl, unwrap } from "@/api/client";
import { CleanPicture } from "@/components/CleanPicture";
import { OutfitRow } from "@/components/SavedOutfits";
import { TagForm } from "@/components/TagForm";
import { WornTodayButton } from "@/components/WornTodayButton";
import { pickTags, type Tags } from "@/lib/tags";
import { section, STATUS_TEXT } from "@/lib/wardrobe";

type Extra = { brand: string; purchase_price: string; purchase_date: string; notes: string };
const DAY = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" });

/** One piece, edited in full: its picture and status on one side, its tags, where it was bought and its history on the other. */
export default function ItemDetail() {
  const id = Number(useParams().id);
  const qc = useQueryClient();
  const navigate = useNavigate();
  const detail = useQuery({
    queryKey: ["item", id],
    queryFn: () => unwrap(api.GET("/api/items/{item_id}", { params: { path: { item_id: id } } })),
  });
  const [tags, setTags] = useState<Tags | null>(null);
  const [extra, setExtra] = useState<Extra>({ brand: "", purchase_price: "", purchase_date: "", notes: "" });

  useEffect(() => {
    const it = detail.data?.item;
    if (!it) return;
    setTags(pickTags(it));
    setExtra({
      brand: it.brand ?? "",
      purchase_price: it.purchase_price?.toString() ?? "",
      purchase_date: it.purchase_date ?? "",
      notes: it.notes ?? "",
    });
  }, [detail.data]);

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["item", id] });
    void qc.invalidateQueries({ queryKey: ["items"] });
  };
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        api.PATCH("/api/items/{item_id}", {
          params: { path: { item_id: id } },
          body: {
            ...tags!,
            brand: extra.brand || null,
            purchase_price: extra.purchase_price ? Number(extra.purchase_price) : null,
            purchase_date: extra.purchase_date || null,
            notes: extra.notes,
          },
        }),
      ),
    onSuccess: refresh,
  });
  const retire = useMutation({
    mutationFn: () =>
      unwrap(api.POST("/api/items/{item_id}/status", { params: { path: { item_id: id } }, body: { status: "retired" } })),
    onSuccess: refresh,
  });
  const destroy = useMutation({
    mutationFn: () =>
      unwrap(api.DELETE("/api/items/{item_id}", { params: { path: { item_id: id }, query: { hard: true } } })),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["items"] });
      void navigate("/");
    },
  });

  if (detail.error) {
    const missing = detail.error instanceof ApiError && detail.error.status === 404;
    return (
      <p className="err">
        {missing ? "This piece does not exist any more." : errorMessage(detail.error)}{" "}
        <Link className="lnk" to="/">
          Back to your wardrobe
        </Link>
      </p>
    );
  }
  if (detail.isLoading || !tags) return <p className="sub">Loading…</p>;
  const { item, siblings, corrections, photo_path, wears, outfits } = detail.data!;
  const edited = () => save.isSuccess && save.reset(); // "Saved" must not stay once there are new edits
  const field = (k: keyof Extra, label: string, type = "text") => (
    <div className="fld">
      <label htmlFor={`x-${k}`}>{label}</label>
      <input
        id={`x-${k}`}
        className="in"
        type={type}
        value={extra[k]}
        onChange={(e) => {
          edited();
          setExtra({ ...extra, [k]: e.target.value });
        }}
      />
    </div>
  );
  const actionError = save.error ?? retire.error ?? destroy.error;
  const status = STATUS_TEXT[item.status] ?? (item.status === "retired" ? "Retired" : "In the wardrobe");

  return (
    <div className="item-page">
      <header className="item-head">
        <Link className="lnk back" to="/">
          Back to the wardrobe
        </Link>
        <h1>{item.name}</h1>
        <p className="sub">
          {item.subcategory}, in {section(item.category).name}. {status}
          {corrections.length > 0 && `, ${corrections.length} ${corrections.length === 1 ? "correction" : "corrections"} to the AI’s tags`}.
        </p>
      </header>
      <div className="item-grid">
        <aside className="item-side">
          <div className="hero">
            <img className="pic" src={imageUrl(item.cutout_path)} alt={item.name} />
          </div>
          <div className="row">
            <WornTodayButton key={id} itemId={id} />
            {photo_path && (
              <a className="lnk" href={imageUrl(photo_path)} target="_blank" rel="noreferrer">
                Original photo
              </a>
            )}
          </div>
          <CleanPicture kind="item" id={item.id!} row={item} onChange={refresh} />
          {siblings.length > 0 && (
            <div className="sibs">
              <p className="sub">From the same photo</p>
              <div className="row">
                {siblings.map((s) => (
                  <Link key={s.id} to={`/items/${s.id}`} title={s.name}>
                    <img src={imageUrl(s.thumb_path)} alt={s.name} />
                  </Link>
                ))}
              </div>
            </div>
          )}
        </aside>
        <form
          className="item-main"
          onSubmit={(e) => {
            e.preventDefault();
            if (!save.isPending) save.mutate();
          }}
        >
          <section className="sec" aria-label="Details">
            <h2>Details</h2>
            <TagForm
              value={tags}
              onChange={(t) => {
                edited();
                setTags(t);
              }}
              ai={item.ai_tags}
            />
          </section>
          <section className="sec" aria-label="Bought">
            <h2>Bought</h2>
            <div className="flds three">
              {field("brand", "Brand")}
              {field("purchase_price", "Price", "number")}
              {field("purchase_date", "Date", "date")}
            </div>
            <div className="fld wide">
              <label htmlFor="x-notes">Notes</label>
              <textarea
                id="x-notes"
                className="in ta"
                rows={3}
                value={extra.notes}
                onChange={(e) => {
                  edited();
                  setExtra({ ...extra, notes: e.target.value });
                }}
              />
            </div>
          </section>
          {actionError && <p className="err">{errorMessage(actionError)}</p>}
          <div className="row">
            <button type="submit" className="btn primary" disabled={save.isPending}>
              {save.isPending ? "Saving…" : save.isSuccess ? "Saved" : "Save"}
            </button>
            {item.status !== "retired" && (
              <button type="button" className="btn" disabled={retire.isPending} onClick={() => retire.mutate()}>
                Retire
              </button>
            )}
            <button
              type="button"
              className="btn ghost danger"
              disabled={destroy.isPending}
              onClick={() => window.confirm(`Permanently delete "${item.name}" and its images?`) && destroy.mutate()}
            >
              Delete permanently
            </button>
          </div>
          <section className="sec" aria-label="Worn">
            <h2>Worn</h2>
            {wears.length === 0 && <p className="sub">Not worn yet.</p>}
            <ul className="wears">
              {wears.map((w) => (
                <li key={w.id}>
                  <time dateTime={w.worn_on}>{DAY.format(new Date(w.worn_on))}</time>
                  <span>
                    {w.outfit_id == null ? "on its own" : "in an outfit"}
                    {w.audience_tags.length > 0 && `, for ${w.audience_tags.join(", ")}`}
                  </span>
                </li>
              ))}
            </ul>
          </section>
          <section className="sec" aria-label="In outfits">
            <h2>In outfits</h2>
            {outfits.length === 0 && <p className="sub">Not in any saved look yet.</p>}
            <ul className="keeps">
              {outfits.map((o) => (
                <OutfitRow key={o.id} outfit={o} />
              ))}
            </ul>
          </section>
        </form>
      </div>
    </div>
  );
}
