import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { CATEGORIES, PATTERNS, SEASONS, changedFromAi, type Color, type Tags } from "@/lib/tags";

const selectCls = "h-9 w-full rounded-md border bg-background px-2 text-sm";
const MAX_COLORS = 3;

type Props = { value: Tags; onChange: (t: Tags) => void; ai?: object | null };

export function TagForm({ value, onChange, ai }: Props) {
  const set = <K extends keyof Tags>(k: K, v: Tags[K]) => onChange({ ...value, [k]: v });
  const label = (k: keyof Tags, text: string) => (
    <Label className="flex items-center gap-2">
      {text}
      {changedFromAi(value, ai, k) && <Badge variant="secondary">changed from AI</Badge>}
    </Label>
  );
  const setColors = (colors: Color[]) =>
    set("colors", colors.map((c, i) => ({ ...c, role: i === 0 ? "primary" : "secondary" })));

  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <div className="grid gap-1 sm:col-span-2">
        {label("name", "Name")}
        <Input value={value.name} onChange={(e) => set("name", e.target.value)} />
      </div>
      <div className="grid gap-1">
        {label("category", "Category")}
        <select className={selectCls} value={value.category} onChange={(e) => set("category", e.target.value as Tags["category"])}>
          {CATEGORIES.map((c) => <option key={c}>{c}</option>)}
        </select>
      </div>
      <div className="grid gap-1">
        {label("subcategory", "Subcategory")}
        <Input value={value.subcategory} onChange={(e) => set("subcategory", e.target.value)} />
      </div>
      <div className="grid gap-1">
        {label("pattern", "Pattern")}
        <select className={selectCls} value={value.pattern} onChange={(e) => set("pattern", e.target.value as Tags["pattern"])}>
          {PATTERNS.map((p) => <option key={p}>{p}</option>)}
        </select>
      </div>
      <div className="grid gap-1">
        {label("fabric", "Fabric")}
        <Input value={value.fabric ?? ""} onChange={(e) => set("fabric", e.target.value || null)} />
      </div>
      <div className="grid gap-1">
        {label("fit", "Fit")}
        <Input value={value.fit ?? ""} onChange={(e) => set("fit", e.target.value || null)} />
      </div>
      {(["formality", "warmth"] as const).map((k) => (
        <div key={k} className="grid gap-1">
          {label(k, `${k[0].toUpperCase()}${k.slice(1)}: ${value[k]}`)}
          <input type="range" min={1} max={5} value={value[k]} onChange={(e) => set(k, Number(e.target.value))} />
        </div>
      ))}
      <div className="grid gap-1 sm:col-span-2">
        {label("seasons", "Seasons")}
        <div className="flex flex-wrap gap-3">
          {SEASONS.map((s) => (
            <label key={s} className="flex items-center gap-1 text-sm">
              <input
                type="checkbox"
                checked={value.seasons.includes(s)}
                onChange={(e) => set("seasons", e.target.checked ? [...value.seasons, s] : value.seasons.filter((x) => x !== s))}
              />
              {s}
            </label>
          ))}
        </div>
      </div>
      <div className="grid gap-1 sm:col-span-2">
        {label("colors", "Colors (first is primary)")}
        <div className="flex flex-wrap gap-2">
          {value.colors.map((c, i) => (
            <div key={i} className="flex items-center gap-1 rounded-md border p-1">
              <input
                type="color"
                value={c.hex}
                aria-label={`Color ${i + 1}`}
                onChange={(e) => setColors(value.colors.map((x, j) => (j === i ? { ...x, hex: e.target.value } : x)))}
              />
              <Input
                className="h-8 w-24"
                value={c.name}
                onChange={(e) => setColors(value.colors.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))}
              />
              {value.colors.length > 1 && (
                <Button variant="ghost" size="sm" onClick={() => setColors(value.colors.filter((_, j) => j !== i))} aria-label="Remove color">
                  ×
                </Button>
              )}
            </div>
          ))}
          {value.colors.length < MAX_COLORS && (
            <Button variant="outline" size="sm" onClick={() => setColors([...value.colors, { hex: "#888888", name: "grey", role: "secondary" }])}>
              + color
            </Button>
          )}
        </div>
      </div>
      <div className="grid gap-1 sm:col-span-2">
        {label("tags", "Tags (comma separated)")}
        <Input
          key={value.tags.join(",")}
          defaultValue={value.tags.join(", ")}
          onBlur={(e) => set("tags", e.target.value.split(",").map((t) => t.trim()).filter(Boolean))}
        />
      </div>
    </div>
  );
}
