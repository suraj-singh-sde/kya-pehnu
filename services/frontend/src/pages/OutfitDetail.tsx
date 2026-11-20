import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api, errorMessage, unwrap, type OutfitOut } from "@/api/client";
import { MannequinChat, MannequinView, useMannequin, useMannequinMode } from "@/components/MannequinPicture";
import { Collage, localToday, WEAR_REASON_MAX } from "@/components/outfitHelpers";
import { wornSummary } from "@/components/SavedOutfits";
import { Thumbs } from "@/components/Thumbs";
import { ItemPic } from "@/components/wardrobe/GarmentIcons";
import { chatErrorText } from "@/lib/chat";

const DAY = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" });
const REFRESH = ["outfits", "items", "item"];

/** A saved look, opened: on the mannequin, its pieces, why it was picked and how often it was worn. Wear again logs today. */
export default function OutfitDetail() {
  const id = Number(useParams().id);
  const qc = useQueryClient();
  const { mode } = useMannequinMode();
  // ponytail: picked out of the saved list, which the rail has already fetched; a GET /api/outfits/{id} if the list ever pages
  const outfits = useQuery({ queryKey: ["outfits"], queryFn: () => unwrap(api.GET("/api/outfits")) });
  const outfit = outfits.data?.find((o) => o.id === id);
  const wearable = !!outfit && !outfit.incomplete && outfit.items.every((i) => i.status === "available");
  const mannequin = useMannequin(outfit?.item_ids ?? [], outfit && !outfit.incomplete ? mode : "off");
  const wear = useMutation({
    mutationFn: (o: OutfitOut) =>
      unwrap(api.POST("/api/outfits/wear", { body: { item_ids: o.item_ids, worn_on: localToday(), reason: o.reason?.slice(0, WEAR_REASON_MAX) ?? null } })),
    onSuccess: () => Promise.all(REFRESH.map((k) => qc.invalidateQueries({ queryKey: [k] }))),
  });

  if (outfits.isLoading) return <p className="sub">Loading…</p>;
  if (outfits.error || !outfit) {
    return (
      <p className="err">
        {outfits.error ? errorMessage(outfits.error) : "This look is not saved any more."}{" "}
        <Link className="lnk" to="/chat">
          Back to the stylist
        </Link>
      </p>
    );
  }
  const names = outfit.items.map((i) => i.name).join(", ");
  return (
    <div className="item-page">
      <header className="item-head">
        <Link className="lnk back" to="/chat">
          Back to the stylist
        </Link>
        <h1>{outfit.name}</h1>
        <p className="sub">
          {wornSummary(outfit)}. Saved {DAY.format(new Date(outfit.created_at))}
          {outfit.occasion_tags.length > 0 && ` for ${outfit.occasion_tags.join(", ")}`}.
        </p>
      </header>
      <div className="item-grid">
        <aside className="item-side">
          <div className="look-pic">
            <MannequinView m={mannequin} fallback={<Collage items={outfit.items} />} alt={`On a mannequin: ${names}`} className="look-img" />
          </div>
          <MannequinChat m={mannequin} />
        </aside>
        <article className="look" aria-label={`Pieces of ${outfit.name}`}>
          <ul className="pieces">
            {outfit.items.map((item) => (
              <li key={item.id}>
                <ItemPic item={item} />
                <span>
                  <Link className="lnk" to={`/items/${item.id}`}>
                    {item.name}
                  </Link>
                  {item.status !== "available" && <em className="pill" style={{ marginLeft: 6 }}>{item.status}</em>}
                </span>
              </li>
            ))}
            {outfit.incomplete && (
              <li>
                <span className="pic" />
                <span>An item was deleted</span>
              </li>
            )}
          </ul>
          {outfit.reason && <p className="why">{outfit.reason}</p>}
          {!wearable && <p className="hint">Some pieces are gone or not available, so this look cannot be worn now.</p>}
          {wear.error && (
            <p role="alert" className="err">
              {chatErrorText(wear.error)}
            </p>
          )}
          {wear.isSuccess ? (
            <div className="done" role="status">
              <p>Logged as worn today.</p>
            </div>
          ) : (
            <div className="look-foot">
              <button className="btn primary" aria-label={`Wear again: ${names}`} disabled={!wearable || wear.isPending} onClick={() => wear.mutate(outfit)}>
                {wear.isPending ? "Saving…" : "Wear again"}
              </button>
              <Thumbs outfitId={outfit.id} rating={outfit.rating} />
            </div>
          )}
        </article>
      </div>
    </div>
  );
}
