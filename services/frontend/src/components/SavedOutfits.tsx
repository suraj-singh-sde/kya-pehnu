import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, errorMessage, imageUrl, unwrap, type OutfitOut } from "@/api/client";
import { Thumbs } from "@/components/Thumbs";
import { ItemPic } from "@/components/wardrobe/GarmentIcons";

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
const DAY = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short" });

/** "Worn 2 times, last 1 Oct, for Priya": parts without data are left out. */
export function wornSummary(o: OutfitOut): string {
  return [
    `Worn ${plural(o.times_worn, "time", "times")}`,
    o.last_worn && `last ${DAY.format(new Date(o.last_worn))}`,
    o.audiences.length > 0 && `for ${o.audiences.join(", ")}`,
  ]
    .filter(Boolean)
    .join(", ");
}

/** One saved look, opening on a tap: the collage, or the pictures of the items that still exist (an incomplete outfit has no collage). */
export function OutfitRow({ outfit }: { outfit: OutfitOut }) {
  const to = `/outfits/${outfit.id}`;
  return (
    <li className="keep">
      {/* the picture is a second way to the same page: the name is the link a screen reader or the Tab key lands on */}
      <Link className="keep-left" to={to} tabIndex={-1} aria-hidden="true">
        {outfit.collage_path && !outfit.incomplete ? (
          <img src={imageUrl(outfit.collage_path)} alt="" className="keep-pic" />
        ) : (
          <div className="keep-pic strip" style={{ padding: 6, gap: 4, flexWrap: "wrap" }}>
            {outfit.items.map((i) => (
              <figure key={i.id} style={{ flexBasis: 20 }}>
                <ItemPic item={i} />
              </figure>
            ))}
          </div>
        )}
      </Link>
      <div className="keep-body">
        <Link className="keep-name" to={to}>
          {outfit.name}
        </Link>
        <span className="sub">{wornSummary(outfit)}</span>
        {outfit.incomplete && <span className="pill">An item was deleted</span>}
        <Thumbs outfitId={outfit.id} rating={outfit.rating} />
      </div>
    </li>
  );
}

export function SavedOutfits() {
  const outfits = useQuery({ queryKey: ["outfits"], queryFn: () => unwrap(api.GET("/api/outfits")) });
  return (
    <section className="saved" aria-labelledby="saved-outfits">
      <h2 id="saved-outfits" className="rail-h">
        Saved looks
      </h2>
      {outfits.isLoading && <p className="sub">Loading…</p>}
      {outfits.error && <p className="err">{errorMessage(outfits.error)}</p>}
      {outfits.data?.length === 0 && <p className="sub" style={{ margin: 0 }}>Wear a look the stylist suggests and it is kept here.</p>}
      <ul className="keeps">
        {outfits.data?.map((o) => <OutfitRow key={o.id} outfit={o} />)}
      </ul>
    </section>
  );
}
