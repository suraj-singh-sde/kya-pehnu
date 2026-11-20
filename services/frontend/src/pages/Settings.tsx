import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type KeyboardEvent } from "react";
import { Link } from "react-router-dom";
import { api, errorMessage, unwrap, type Profile } from "@/api/client";
import { LlmForm } from "@/components/LlmForm";
import { compressImage, setFrame, useFrame } from "@/lib/frame";
import { checkPlace, cityOf, UnknownPlaceError } from "@/lib/place";
import { isDark, toggleTheme } from "@/lib/theme";
import { FINISHES, type Finish } from "@/lib/wardrobe";

const loadSettings = () => unwrap(api.GET("/api/settings"));
const SAVED_FOR_MS = 2500;

type Text = "name" | "home_city" | "skin_tone" | "fit_prefs";
type Area = "body_notes" | "style_prefs";
type Num = "height_cm" | "weight_kg";
type Colors = "colors_loved" | "colors_avoided";

/** A colour name the browser knows ("navy", "sky blue") gets a swatch; any other word is kept as typed. */
const known = (name: string): boolean => typeof CSS !== "undefined" && CSS.supports("color", name.trim().toLowerCase().replace(/\s+/g, ""));

/** Colour names as chips: Enter or a comma adds what is typed, Backspace on an empty box takes the last one off. */
function ColorList({ id, label, value, onChange }: { id: string; label: string; value: string[]; onChange: (v: string[]) => void }) {
  const [draft, setDraft] = useState("");
  const add = () => {
    const names = draft
      .split(",")
      .map((s) => s.trim())
      .filter((n) => n && !value.includes(n));
    if (names.length) onChange([...value, ...names]);
    setDraft("");
  };
  const key = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      add();
    } else if (e.key === "Backspace" && !draft && value.length) onChange(value.slice(0, -1));
  };
  return (
    <div className="fld">
      <label htmlFor={id}>{label}</label>
      <div className="colors">
        {value.map((c) => (
          <span key={c} className="color">
            {known(c) && <i style={{ background: c }} aria-hidden="true" />}
            {c}
            <button type="button" aria-label={`Remove ${c}`} onClick={() => onChange(value.filter((x) => x !== c))}>
              ×
            </button>
          </span>
        ))}
        <input id={id} value={draft} placeholder={value.length ? "Add another" : "navy, olive, rust"} autoComplete="off" onChange={(e) => setDraft(e.target.value)} onKeyDown={key} onBlur={add} />
      </div>
    </div>
  );
}

/** The photo in the frame on the wall (desktop). It stays in this browser only. */
function FrameRow() {
  const src = useFrame();
  const [msg, setMsg] = useState("");
  const change = async (file: File | undefined) => {
    if (!file) return;
    try {
      const data = await compressImage(file);
      setMsg(setFrame(data) ? "Photo changed" : "Could not save in this browser, showing it for now");
    } catch (e) {
      setMsg(errorMessage(e));
    }
  };
  return (
    <div className="frame-row desk">
      <img src={src} alt="The photo in the frame" />
      <div>
        The photo in the frame on the wall. It stays in this browser.
        <div className="row">
          <label className="lnk file">
            Change photo
            <input
              type="file"
              accept="image/*"
              onChange={(e) => {
                void change(e.target.files?.[0]);
                e.target.value = "";
              }}
            />
          </label>
          <button
            type="button"
            className="lnk"
            onClick={() => {
              setFrame(null);
              setMsg("Back to the default photo");
            }}
          >
            Use default
          </button>
          <span role="status" className="okline">
            {msg}
          </span>
        </div>
      </div>
    </div>
  );
}

/**
 * Account: who you are and how the almirah looks on one side, what the stylist and the mannequin know about you on the
 * other. The profile is one form with one save bar that appears when something changed. Fields the app never reads
 * (age, budget) are not asked; the shelves are arranged on the Wardrobe page.
 */
export default function Settings() {
  const qc = useQueryClient();
  const info = useQuery({ queryKey: ["settings"], queryFn: loadSettings });
  const profile = useQuery({ queryKey: ["profile"], queryFn: () => unwrap(api.GET("/api/profile")) });
  const items = useQuery({ queryKey: ["items", "all"], queryFn: () => unwrap(api.GET("/api/items")) });
  const me = useQuery({ queryKey: ["me"], queryFn: () => unwrap(api.GET("/api/auth/me")) });
  const signOut = useMutation({
    mutationFn: () => unwrap(api.POST("/api/auth/logout")),
    onSuccess: () => {
      qc.clear();
      window.location.assign("/signin");
    },
  });
  const [form, setFormState] = useState<Profile | null>(null);
  const setForm = (next: Profile) => {
    if (save.isSuccess) save.reset(); // "Saved" must not stay once there are new edits
    setFormState(next);
  };
  useEffect(() => {
    if (profile.data) setFormState(profile.data);
  }, [profile.data]);

  const save = useMutation({
    mutationFn: async (keepCity: boolean) => {
      const { id: _id, ...body } = form!;
      if (body.home_city !== profile.data?.home_city && !keepCity) await checkPlace(body.home_city);
      return unwrap(api.PUT("/api/profile", { body }));
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["profile"] }),
  });
  // "Saved" shows for a moment, then the bar goes away.
  useEffect(() => {
    if (!save.isSuccess) return;
    const t = setTimeout(() => save.reset(), SAVED_FOR_MS);
    return () => clearTimeout(t);
  }, [save.isSuccess]); // eslint-disable-line react-hooks/exhaustive-deps
  const [dark, setDark] = useState(isDark);
  // The finish is saved as soon as a sample is picked: a one-tap choice, not part of the profile form.
  const finish = useMutation({
    mutationFn: (theme: Finish) => {
      const { id: _id, ...base } = profile.data!;
      return unwrap(api.PUT("/api/profile", { body: { ...base, theme } }));
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["profile"] }),
  });

  if (!form) return <p className="sub">Loading…</p>;
  const dirty = !!profile.data && JSON.stringify(form) !== JSON.stringify(profile.data);
  const set = (patch: Partial<Profile>) => setForm({ ...form, ...patch });
  const text = (k: Text, label: string, hint?: string) => (
    <div className="fld">
      <label htmlFor={k}>{label}</label>
      <input id={k} className="in" value={form[k] ?? ""} placeholder={hint} autoComplete="off" onChange={(e) => set({ [k]: e.target.value })} />
    </div>
  );
  const area = (k: Area, label: string, hint: string) => (
    <div className="fld wide">
      <label htmlFor={k}>{label}</label>
      <textarea id={k} className="in ta" rows={3} value={form[k] ?? ""} placeholder={hint} onChange={(e) => set({ [k]: e.target.value })} />
    </div>
  );
  // Empty means "not known"; the mannequin picture uses these for its build and is never a photo of you.
  const num = (k: Num, label: string, min: number, max: number, unit: string) => (
    <div className="fld">
      <label htmlFor={k}>{label}</label>
      <div className="unit">
        <input id={k} className="in" type="number" inputMode="numeric" min={min} max={max} value={form[k] ?? ""} onChange={(e) => set({ [k]: e.target.value === "" ? null : Number(e.target.value) })} />
        <span>{unit}</span>
      </div>
    </div>
  );
  const colors = (k: Colors, label: string) => <ColorList id={k} label={label} value={form[k] ?? []} onChange={(v) => set({ [k]: v })} />;
  const pieces = items.data?.length;

  return (
    <div className="acct">
      <aside className="acct-side">
        <section className="sec who" aria-label="You">
          {/* Always our own initial: Google's picture is a loud orange letter for an account without a photo. */}
          <span className="avatar" aria-hidden="true">
            {(form.name || "K")[0].toUpperCase()}
          </span>
          <h1>{form.name || "Your wardrobe"}</h1>
          {me.data && <p className="mail">{me.data.email}</p>}
          <p className="who-line">
            {form.gender === "female" ? "A woman’s almirah" : "A man’s almirah"}
            {pieces != null && ` with ${pieces} ${pieces === 1 ? "piece" : "pieces"}`}
            {form.home_city && `, in ${form.home_city}`}.
          </p>
          <button className="btn ghost sm" type="button" disabled={signOut.isPending} onClick={() => signOut.mutate()}>
            Sign out
          </button>
          {signOut.isError && <p className="err">Could not sign out. Try again.</p>}
        </section>

        <section className="sec" aria-label="Appearance">
          <h2>Appearance</h2>
          <div className="fins" role="group" aria-label="Almirah finish">
            {FINISHES.map((f) => (
              <button
                key={f.id}
                type="button"
                className="fin"
                aria-pressed={(profile.data?.theme ?? "teal") === f.id}
                disabled={finish.isPending}
                style={{ "--s": f.swatch } as React.CSSProperties}
                onClick={() => finish.mutate(f.id)}
              >
                <i aria-hidden="true" />
                <span>{f.label}</span>
              </button>
            ))}
          </div>
          {finish.error && <p className="err">{errorMessage(finish.error)}</p>}
          <label className="switch">
            <span>Dark mode</span>
            <input type="checkbox" role="switch" className="sw-in" checked={dark} onChange={() => setDark(toggleTheme())} />
          </label>
          <p className="sub desk" style={{ margin: 0 }}>
            The shelves are arranged on the <Link className="lnk" to="/">Wardrobe</Link> page: open the almirah and choose Arrange almirah.
          </p>
          <FrameRow />
        </section>
      </aside>

      <form
        className="acct-main"
        onSubmit={(e) => {
          e.preventDefault();
          if (dirty && !save.isPending) save.mutate(false);
        }}
      >
        <section className="sec" aria-label="About you">
          <h2>About you</h2>
          <div className="flds">
            {text("name", "Name")}
            {text("home_city", "Home city", "For the weather")}
          </div>
        </section>

        <section className="sec" aria-label="What the stylist knows">
          <h2>What the stylist knows</h2>
          <p className="sub">It reads these before every suggestion. Leave blank what you don’t mind about.</p>
          <div className="flds">
            {area("style_prefs", "Your style", "Relaxed on weekdays, sharp for meetings. Loves linen, no logos.")}
            {colors("colors_loved", "Colours you love")}
            {colors("colors_avoided", "Colours you avoid")}
            {text("fit_prefs", "Fit", "Slim shirts, straight trousers")}
            {area("body_notes", "Body notes", "Broad shoulders, long arms")}
          </div>
        </section>

        <section className="sec" aria-label="For the mannequin">
          <h2>For the mannequin</h2>
          <p className="sub">Outfit pictures are drawn on a mannequin built from these. It is never a photo of you.</p>
          <div className="flds three">
            {text("skin_tone", "Skin tone", "Warm medium")}
            {num("height_cm", "Height", 100, 250, "cm")}
            {num("weight_kg", "Weight", 25, 300, "kg")}
          </div>
        </section>

        {save.error && <p className="err">{errorMessage(save.error)}</p>}
        {save.error instanceof UnknownPlaceError && (save.error.suggestions.length > 0 || save.error.keepable) && (
          <div className="chips" aria-label="Did you mean">
            {save.error.suggestions.map((n) => (
              <button key={n} type="button" className="btn sm" onClick={() => set({ home_city: cityOf(n) })}>
                {n}
              </button>
            ))}
            {save.error.keepable && (
              <button type="button" className="btn sm ghost" onClick={() => save.mutate(true)}>
                Keep “{form.home_city}”
              </button>
            )}
          </div>
        )}
        <div className={`savebar${dirty || save.isSuccess ? " on" : ""}`} role="status">
          <span>{save.isSuccess && !dirty ? "Saved" : "Unsaved changes"}</span>
          {dirty && (
            <button type="button" className="btn sm ghost" onClick={() => setFormState(profile.data!)}>
              Discard
            </button>
          )}
          <button type="submit" className="btn sm primary" disabled={!dirty || save.isPending}>
            {save.isPending ? "Saving…" : "Save"}
          </button>
        </div>
      </form>

      {info.data?.llm.can_edit && (
        <section className="sec acct-wide" aria-label="AI provider">
          <h2>AI provider</h2>
          <p className="sub">The model that reads photos and runs the stylist, for everyone on this Kya Pehnu. Photos are sent to the provider you pick.</p>
          <LlmForm key={info.data.llm.active_provider ?? ""} llm={info.data.llm} />
        </section>
      )}
    </div>
  );
}
