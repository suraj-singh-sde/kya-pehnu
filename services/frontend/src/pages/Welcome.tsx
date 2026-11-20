import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api, errorMessage, unwrap, type Profile } from "@/api/client";
import { Bot, Bubble } from "@/components/ChatThread";
import { checkPlace, cityOf, UnknownPlaceError } from "@/lib/place";

/**
 * Chat-style onboarding: one question at a time, typed out, answered in the box below. The answers become the
 * profile; gender is asked once here and fixes which almirah the person gets.
 */
type Answers = { name: string; age: number | null; gender: "male" | "female" | ""; height_cm: number | null; weight_kg: number | null; home_city: string };
type Step = {
  key: keyof Answers;
  ask: (a: Answers) => string;
  kind: "text" | "number" | "choice";
  choices?: { value: string; label: string }[];
  placeholder?: string;
  optional?: boolean;
  parse: (raw: string) => string | number | null | undefined; // undefined = not valid
  invalid?: string;
};

const num = (lo: number, hi: number) => (raw: string) => {
  const n = Number(raw.trim());
  return Number.isInteger(n) && n >= lo && n <= hi ? n : undefined;
};

const STEPS: Step[] = [
  { key: "name", ask: () => "Hi, I’m your stylist, here to answer your kya pehnu. What should I call you?", kind: "text", placeholder: "Your name", parse: (r) => r.trim().slice(0, 60) || undefined, invalid: "A name, please. Even a short one." },
  { key: "age", ask: (a) => `Nice to meet you, ${a.name}. How old are you?`, kind: "number", placeholder: "Age in years", parse: num(10, 120), invalid: "A number between 10 and 120." },
  {
    key: "gender",
    ask: () => "Whose wardrobe is this? It decides how the almirah is laid out and the mannequin’s shape, and it cannot be changed later.",
    kind: "choice",
    choices: [
      { value: "male", label: "A man’s" },
      { value: "female", label: "A woman’s" },
    ],
    parse: (r) => (r === "male" || r === "female" ? r : undefined),
  },
  { key: "height_cm", ask: () => "How tall are you, in centimetres? Skip if you’d rather not say.", kind: "number", placeholder: "Height in cm", optional: true, parse: num(100, 250), invalid: "A number between 100 and 250 cm, or skip." },
  { key: "weight_kg", ask: () => "And your weight, in kilograms? The mannequin uses it for its build. Skip is fine.", kind: "number", placeholder: "Weight in kg", optional: true, parse: num(25, 300), invalid: "A number between 25 and 300 kg, or skip." },
  { key: "home_city", ask: () => "Last one: which city do you live in? I check its weather before suggesting outfits.", kind: "text", placeholder: "City", parse: (r) => r.trim().slice(0, 80) || undefined, invalid: "I need a city for the weather." },
];
const DONE = (a: Answers) => `That’s everything, ${a.name}. Your almirah is ready. Add a few pieces and ask me what to wear.`;
const SKIP = "skip";
const same = (a: string, b: string) => !!b && a.trim().toLowerCase() === b.trim().toLowerCase();

type Line = { who: "bot" | "me"; text: string };
const reduce = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export default function Welcome() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const profile = useQuery({ queryKey: ["profile"], queryFn: () => unwrap(api.GET("/api/profile")) });
  const me = useQuery({ queryKey: ["me"], queryFn: () => unwrap(api.GET("/api/auth/me")) });
  // Google already told us the name: it is offered in the box, so Enter keeps it and typing replaces it.
  const first = (me.data?.display_name ?? "").split(" ")[0];
  const suggestedName = first.includes("@") ? "" : first.slice(0, 60);
  const [answers, setAnswers] = useState<Answers>({ name: "", age: null, gender: "", height_cm: null, weight_kg: null, home_city: "" });
  const [step, setStep] = useState(0);
  const [lines, setLines] = useState<Line[]>([]);
  const [typing, setTyping] = useState(""); // the bot line being typed out
  const [text, setText] = useState("");
  const [hint, setHint] = useState("");
  const [near, setNear] = useState<string[]>([]); // "did you mean" cities
  const [keepable, setKeepable] = useState(""); // a spelling the geocoder matched loosely; the person may keep it
  const box = useRef<HTMLInputElement>(null);
  const end = useRef<HTMLDivElement>(null);
  const current = STEPS[step] as Step | undefined;
  const finished = step >= STEPS.length;
  // Answers are taken only while the bot's question is the last line: not during typing, and not in the moment
  // between an answer and the next question (a fast typist's keystrokes would be lost there).
  const waiting = !!typing || lines[lines.length - 1]?.who !== "bot";

  // Type the current question out, word by word.
  useEffect(() => {
    const full = finished ? DONE(answers) : current!.ask(answers);
    if (reduce()) {
      setLines((l) => [...l, { who: "bot", text: full }]);
      return;
    }
    const words = full.split(" ");
    let i = 0;
    setTyping("");
    const t = setInterval(() => {
      i++;
      setTyping(words.slice(0, i).join(" "));
      if (i >= words.length) {
        clearInterval(t);
        setTyping("");
        setLines((l) => [...l, { who: "bot", text: full }]);
      }
    }, 45);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step]);
  // The box is disabled while a question types out; focus it once that render is done, so nobody has to click it.
  useEffect(() => {
    if (!waiting && !finished && current?.kind !== "choice") box.current?.focus({ preventScroll: true });
    if (!waiting && current?.key === "name" && suggestedName) {
      setText(suggestedName);
      requestAnimationFrame(() => box.current?.select());
    }
  }, [waiting, step, finished, current, suggestedName]);
  // Keep the newest line in view above the sticky composer, after the new line has been laid out.
  useEffect(() => {
    const id = requestAnimationFrame(() => window.scrollTo({ top: document.documentElement.scrollHeight }));
    return () => cancelAnimationFrame(id);
  }, [lines, typing]);

  const save = useMutation({
    mutationFn: (a: Answers) => {
      const { id: _id, ...base } = profile.data!;
      return unwrap(api.PUT("/api/profile", { body: { ...base, ...a, gender: a.gender as Profile["gender"] } }));
    },
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ["profile"] });
      navigate("/", { replace: true });
    },
  });

  const [checking, setChecking] = useState(false);
  const answer = async (raw: string) => {
    if (!current || typing || checking) return;
    const skipped = current.optional && (raw.trim() === "" || raw.trim().toLowerCase() === SKIP);
    const value = skipped ? null : current.parse(raw);
    if (value === undefined) {
      setHint(current.invalid ?? "Try again.");
      return;
    }
    if (current.key === "home_city" && !same(String(value), keepable)) {
      setChecking(true);
      try {
        await checkPlace(String(value));
      } catch (e) {
        setHint(errorMessage(e));
        setNear(e instanceof UnknownPlaceError ? e.suggestions : []);
        setKeepable(e instanceof UnknownPlaceError && e.keepable ? String(value) : "");
        return;
      } finally {
        setChecking(false);
      }
    }
    const shown = current.kind === "choice" ? current.choices!.find((c) => c.value === raw)?.label ?? raw : skipped ? "Skip" : raw.trim();
    const next = { ...answers, [current.key]: value } as Answers;
    setAnswers(next);
    setLines((l) => [...l, { who: "me", text: shown }]);
    setText("");
    setHint("");
    setNear([]);
    setKeepable("");
    setStep(step + 1);
  };
  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    void answer(text);
  };

  return (
    <div className="welcome">
      <header>
        <h1>Welcome</h1>
        <p className="sub">A few questions, then your almirah.</p>
      </header>
      <div className="thread" aria-live="polite">
        {lines.map((l, i) =>
          l.who === "me" ? (
            <Bubble key={i}>{l.text}</Bubble>
          ) : (
            <Bot key={i}>
              <p className="say">{l.text}</p>
            </Bot>
          ),
        )}
        {typing && (
          <Bot>
            <p className="say">{typing}</p>
          </Bot>
        )}
        <div ref={end} />
      </div>
      <div className="composer">
        {hint && (
          <p role="alert" className="err">
            {hint}
          </p>
        )}
        {(near.length > 0 || keepable) && (
          <div className="sugs" aria-label="Did you mean">
            {near.map((n) => (
              <button key={n} type="button" className="btn sm" onClick={() => void answer(cityOf(n))}>
                {n}
              </button>
            ))}
            {keepable && (
              <button type="button" className="btn sm ghost" onClick={() => void answer(keepable)}>
                Keep “{keepable}”
              </button>
            )}
          </div>
        )}
        {save.error && (
          <p role="alert" className="err">
            {errorMessage(save.error)}
          </p>
        )}
        {finished ? (
          <div className="row">
            <button className="btn primary" disabled={!!typing || save.isPending || !profile.data} onClick={() => save.mutate(answers)}>
              {save.isPending ? "Setting up…" : "Open my almirah"}
            </button>
          </div>
        ) : current?.kind === "choice" ? (
          <div className="sugs">
            {current.choices!.map((c) => (
              <button key={c.value} type="button" className="btn" disabled={!!typing} onClick={() => void answer(c.value)}>
                {c.label}
              </button>
            ))}
          </div>
        ) : (
          <form onSubmit={onSubmit}>
            <input
              ref={box}
              className="in"
              type={current?.kind === "number" ? "number" : "text"}
              inputMode={current?.kind === "number" ? "numeric" : undefined}
              placeholder={current?.placeholder}
              aria-label={current?.placeholder}
              autoComplete="off"
              value={text}
              disabled={waiting}
              onChange={(e) => setText(e.target.value)}
            />
            {current?.optional && (
              <button type="button" className="btn" disabled={!!typing} onClick={() => void answer(SKIP)}>
                Skip
              </button>
            )}
            <button className="btn primary" disabled={!!typing || checking || (!current?.optional && !text.trim())}>
              {checking ? "Checking…" : "Send"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
