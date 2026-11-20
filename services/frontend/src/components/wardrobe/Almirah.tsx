import type { Item } from "@/api/client";
import { ItemPic, SectionIcon } from "@/components/wardrobe/GarmentIcons";
import { Clothesline } from "@/components/wardrobe/WardrobeHeader";
import { CATEGORIES, type Category } from "@/lib/tags";
import type { Match } from "@/lib/wardrobe";
import {
  CATEGORY_LABEL,
  MATCH_CATS,
  MATCH_LABEL,
  MAX_SHELVES,
  holds,
  STATUS_TEXT,
  type Kind,
  type Layout,
  type Section,
  type Shelf,
} from "@/lib/wardrobe";

/** Arrange mode: the shelves being edited in place. Section `s<i>` is `shelves[i]`; the trailing "Everything else" shelf has no index. */
export type Arrange = {
  shelves: Shelf[];
  onChange: (shelves: Shelf[]) => void;
};

type Props = {
  items: Item[];
  layout: Layout;
  doorsOpen: boolean;
  open: string | null; // the section shown large, by category
  hung?: string | null; // on a phone, the section whose pieces hang on the rail above
  selected: number | null; // the item shown in the sheet
  arrange: Arrange | null;
  onDoors: (open: boolean) => void;
  onOpen: (id: string | null) => void;
  onPick: (item: Item) => void;
};

const KINDS: { id: Kind; label: string }[] = [
  { id: "rail", label: "Rail" },
  { id: "shelf", label: "Shelf" },
  { id: "drawer", label: "Drawer" },
  { id: "locker", label: "Locker" },
];

/** The steel almirah: two doors, then eight compartments; one compartment at a time can take over the inside. */
export function Almirah({
  items,
  layout,
  doorsOpen,
  open,
  hung = null,
  selected,
  arrange,
  onDoors,
  onOpen,
  onPick,
}: Props) {
  const o = layout.sections.find((s) => s.id === open) ?? null;
  // Arranging: rows size to their editors and the cabinet grows; otherwise the rows share the fixed height.
  const rowList = arrange
    ? layout.rows.map(() => "auto")
    : layout.rows.map((f, i) => (o ? (i === o.row ? 7 : 1) : f) + "fr");
  if (arrange) rowList.push("auto"); // the "Add shelf" slot
  const rows = rowList.join(" ");
  const cols = [0, 1]
    .map((c) => (o && o.col >= 0 && c === o.col ? 6 : 1) + "fr")
    .join(" ");
  const shelves = arrange?.shelves;

  return (
    <div
      className={`cab${doorsOpen ? " open" : ""}${arrange ? " arrange" : ""}`}
      onClick={(e) => {
        if (!doorsOpen) onDoors(true);
        else if ((e.target as HTMLElement).closest(".door")) {
          onOpen(null);
          onDoors(false);
        }
      }}
    >
      <div
        className="inside"
        style={{ gridTemplateRows: rows, gridTemplateColumns: cols }}
        inert={!doorsOpen}
      >
        {layout.sections.map((s) => {
          const i = shelves ? Number(s.id.slice(1)) : -1;
          const edit =
            shelves && i < shelves.length
              ? { shelf: shelves[i], i, shelves, onChange: arrange!.onChange }
              : null;
          return (
            <Cell
              key={s.id}
              s={s}
              o={o}
              hung={s.id === hung}
              items={items.filter((i) => holds(layout, s, i))}
              selected={selected}
              edit={edit}
              arranging={!!arrange}
              onOpen={onOpen}
              onPick={onPick}
            />
          );
        })}
        {shelves && (
          <button
            type="button"
            className="addshelf"
            style={{ gridRow: layout.rows.length + 1, gridColumn: "1 / -1" }}
            disabled={shelves.length >= MAX_SHELVES}
            onClick={() =>
              arrange!.onChange([
                ...shelves,
                { name: "New shelf", kind: "shelf", width: "full", cats: [] },
              ])
            }
          >
            + Add shelf
          </button>
        )}
      </div>
      <div
        className="door l"
        role="button"
        tabIndex={0}
        aria-label={doorsOpen ? "Close the almirah" : "Open the almirah"}
        onKeyDown={(e) =>
          e.key === "Enter" && (e.currentTarget as HTMLElement).click()
        }
      >
        <div className="f">
          <div className="mirror" />
          <span className="handle" />
        </div>
        <div className="b" />
      </div>
      <div className="door r">
        <div className="f">
          <span className="plate" lang="hi">
            क्या पहनूँ<small lang="en">Kya Pehnu</small>
          </span>
          <span className="handle" />
        </div>
        <div className="b" />
      </div>
    </div>
  );
}

type Edit = {
  shelf: Shelf;
  i: number;
  shelves: Shelf[];
  onChange: (shelves: Shelf[]) => void;
};
type CellProps = {
  s: Section;
  o: Section | null;
  hung: boolean;
  items: Item[];
  selected: number | null;
  edit: Edit | null;
  arranging: boolean;
  onOpen: (id: string | null) => void;
  onPick: (item: Item) => void;
};

function Cell({
  s,
  o,
  hung,
  items,
  selected,
  edit,
  arranging,
  onOpen,
  onPick,
}: CellProps) {
  const isOpen = s === o;
  const squeezed = !!o && !isOpen;
  const thin = squeezed && o!.col >= 0 && s.col >= 0 && s.col !== o!.col;
  const cls = [
    "cell",
    isOpen && "is-open",
    squeezed && "squeezed",
    thin && "thin",
    thin && s.row === o!.row && "tall",
    hung && "hung",
  ]
    .filter(Boolean)
    .join(" ");
  const hasFront = s.kind === "drawer" || s.kind === "locker";
  return (
    <div
      id={`c-${s.id}`}
      className={cls}
      data-kind={s.kind}
      style={
        {
          gridRow: s.row + 1,
          gridColumn: s.col < 0 ? "1 / -1" : s.col + 1,
          "--r": s.row, // the phone's open cascade runs row by row
        } as React.CSSProperties
      }
    >
      {!arranging && isOpen && s.kind === "rail" ? (
        <Clothesline shelf={s} items={items} doors selected={selected} onPick={onPick} onClear={() => onOpen(null)} />
      ) : !arranging && (
        <div className="items" inert={!isOpen} style={{ "--n": Math.max(1, items.length) } as React.CSSProperties}>
          {s.kind === "rail" && (
            // the steel rope the closed compartment's pieces hang from; the CSS sags it with the count
            <svg className="rope" viewBox="0 0 100 60" preserveAspectRatio="none" aria-hidden="true">
              <path d="M2 10Q50 42 98 10" />
              <path className="hi" d="M2 9.3Q50 41.3 98 9.3" />
            </svg>
          )}
          {items.map((it, i) => (
            <button
              key={it.id}
              className={`item${selected === it.id ? " on" : ""}`}
              style={{ "--i": i } as React.CSSProperties}
              onClick={() => onPick(it)}
              aria-label={it.name}
            >
              {s.kind === "rail" && (
                <svg className="wire" viewBox="0 0 10 22" preserveAspectRatio="none" aria-hidden="true">
                  <path d="M5 1v21" />
                </svg>
              )}
              <ItemPic item={it} />
              <span className="cap">
                {it.name}
                {STATUS_TEXT[it.status] && <em>{STATUS_TEXT[it.status]}</em>}
              </span>
            </button>
          ))}
          {isOpen && items.length === 0 && (
            <p className="empty">Nothing here yet.</p>
          )}
        </div>
      )}
      {hasFront && <div className="front" />}
      {edit ? (
        <ShelfEdit {...edit} />
      ) : (
        <div className="label">
          <SectionIcon name={s.icon} />
          <span className="nm">{s.name}</span>
          {arranging ? (
            <span className="ct">
              {s.cats.map((c) => CATEGORY_LABEL[c]).join(", ")} · not on any
              shelf
            </span>
          ) : (
            <span className="ct">{items.length}</span>
          )}
        </div>
      )}
      {!arranging && (
        <button
          className="hit"
          aria-label={`Open ${s.name}, ${items.length} pieces`}
          onClick={() => onOpen(s.id)}
        />
      )}
      <button
        className="x"
        aria-label={`Close ${s.name}`}
        onClick={() => onOpen(null)}
      >
        ×
      </button>
    </div>
  );
}

/** One shelf's controls, drawn inside its compartment: name, kind, width, categories, order, remove. */
function ShelfEdit({ shelf, i, shelves, onChange }: Edit) {
  const update = (patch: Partial<Shelf>) =>
    onChange(shelves.map((s, j) => (j === i ? { ...s, ...patch } : s)));
  const move = (d: -1 | 1) => {
    const next = [...shelves];
    [next[i], next[i + d]] = [next[i + d], next[i]];
    onChange(next);
  };
  // A category lives on one shelf: adding it here takes it off the shelf that had it. A matched shelf (caps) only
  // borrows some pieces, so it neither takes a category off another shelf nor loses one.
  const elsewhere = new Map(
    shelves.flatMap((s, j) => (j === i || s.match || shelf.match ? [] : s.cats.map((c) => [c, s.name] as const))),
  );
  const add = (c: Category) =>
    onChange(
      shelves.map((s, j) =>
        j === i
          ? { ...s, cats: [...s.cats, c] }
          : s.match || shelf.match
            ? s
            : { ...s, cats: s.cats.filter((x) => x !== c) },
      ),
    );
  const free = CATEGORIES.filter((c) => !shelf.cats.includes(c));
  const label = shelf.name || "shelf";
  return (
    <div className="edit" onClick={(e) => e.stopPropagation()}>
      <div className="top">
        <input
          value={shelf.name}
          maxLength={30}
          placeholder="Shelf name"
          aria-label="Shelf name"
          onChange={(e) => update({ name: e.target.value })}
        />
        <button
          type="button"
          className="mv rm"
          disabled={shelves.length === 1}
          aria-label={`Remove ${label}`}
          title="Remove"
          onClick={() => onChange(shelves.filter((_, j) => j !== i))}
        >
          ×
        </button>
      </div>
      <div className="ops">
        <select
          aria-label="Shelf type"
          value={shelf.kind}
          onChange={(e) => update({ kind: e.target.value as Kind })}
        >
          {KINDS.map((k) => (
            <option key={k.id} value={k.id}>
              {k.label}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="mv"
          aria-pressed={shelf.width === "half"}
          title={shelf.width === "half" ? "Half width" : "Full width"}
          onClick={() =>
            update({ width: shelf.width === "half" ? "full" : "half" })
          }
        >
          {shelf.width === "half" ? "Half" : "Full"}
        </button>
        <span className="sp" />
        <button
          type="button"
          className="mv"
          disabled={i === 0}
          aria-label={`Move ${label} up`}
          onClick={() => move(-1)}
        >
          ↑
        </button>
        <button
          type="button"
          className="mv"
          disabled={i === shelves.length - 1}
          aria-label={`Move ${label} down`}
          onClick={() => move(1)}
        >
          ↓
        </button>
      </div>
      <div className="chips">
        {shelf.match && (
          <button
            type="button"
            className="chip on"
            title="Tap to remove"
            onClick={() => update({ cats: [], match: undefined })}
          >
            {MATCH_LABEL[shelf.match]}
          </button>
        )}
        {!shelf.match && shelf.cats.map((c) => (
          <button
            key={c}
            type="button"
            className="chip on"
            title="Tap to remove"
            onClick={() => update({ cats: shelf.cats.filter((x) => x !== c) })}
          >
            {CATEGORY_LABEL[c]}
          </button>
        ))}
        {!shelf.match && (
          <select
            className="chip add"
            value=""
            aria-label={`Add a category to ${label}`}
            onChange={(e) =>
              e.target.value in MATCH_CATS
                ? update({ cats: [MATCH_CATS[e.target.value as Match]], match: e.target.value as Match })
                : e.target.value && add(e.target.value as Category)
            }
          >
            <option value="">+ Add</option>
            {/* Only some pieces of a category, so it takes the shelf over and leaves the rest where they are. */}
            {(Object.keys(MATCH_LABEL) as Match[]).map((m) => (
              <option key={m} value={m}>
                {MATCH_LABEL[m]} (just these on this shelf)
              </option>
            ))}
            {free.map((c) => (
              <option key={c} value={c}>
                {CATEGORY_LABEL[c]}
                {elsewhere.has(c) ? ` (move from ${elsewhere.get(c) || "another shelf"})` : ""}
              </option>
            ))}
          </select>
        )}
        {shelf.cats.length === 0 && (
          <span className="hintc">Pick what goes here</span>
        )}
      </div>
    </div>
  );
}
