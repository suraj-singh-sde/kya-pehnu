import { useMutation, useQuery } from "@tanstack/react-query";
import { useRef, useState, type FormEvent, type ReactNode } from "react";
import { api, ApiError, errorMessage, imageUrl, unwrap } from "@/api/client";
import { chatErrorText } from "@/lib/chat";

export type MannequinMode = "auto" | "button" | "off";

/** Reads the mannequin setting and whether the provider can draw at all. */
export function useMannequinMode(): { mode: MannequinMode; canDraw: boolean } {
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => unwrap(api.GET("/api/settings")) });
  const mode = settings.data?.outfits.mannequin ?? "off";
  const canDraw = settings.data?.clean_pictures ?? false;
  return { mode: canDraw ? mode : "off", canDraw };
}

type Change = { ask: string; path: string };

/**
 * The outfit on a faceless mannequin, and the changes asked of the picture in words. The file is a server-side cache
 * keyed by the item ids, so the query key is the sorted ids: the same outfit on another card or a later visit never
 * asks the model again. Each change redraws the current picture into a new one; going back drops the last.
 */
export function useMannequin(itemIds: number[], mode: MannequinMode) {
  const key = [...itemIds].sort((a, b) => a - b);
  const [requested, setRequested] = useState(false);
  const wanted = mode === "auto" || (mode === "button" && requested);
  const picture = useQuery({
    queryKey: ["mannequin", key],
    queryFn: () => unwrap(api.POST("/api/outfits/mannequin", { body: { item_ids: key } })),
    enabled: mode !== "off" && wanted,
    staleTime: Infinity,
    retry: false,
  });
  const base = picture.data?.path ?? null;
  // The changes belong to the picture they were asked of: a swap brings a new picture and a clean slate.
  const [made, setMade] = useState<{ base: string | null; list: Change[] }>({ base, list: [] });
  const changes = made.base === base ? made.list : [];
  const path = changes.at(-1)?.path ?? base;
  const refine = useMutation({
    mutationFn: (ask: string) => unwrap(api.POST("/api/outfits/mannequin/refine", { body: { path: path!, instruction: ask } })),
    onSuccess: (out, ask) => setMade({ base, list: [...changes, { ask, path: out.path }] }),
  });
  const back = () => {
    refine.reset();
    setMade({ base, list: changes.slice(0, -1) });
  };
  return { mode, picture, path, requested, request: () => setRequested(true), changes, refine, back };
}
export type Mannequin = ReturnType<typeof useMannequin>;

type ViewProps = { m: Mannequin; fallback: ReactNode; alt: string; className?: string };

/** The picture, or the fallback until it is ready and instead of it when it cannot be made. */
export function MannequinView({ m, fallback, alt, className = "" }: ViewProps) {
  if (m.mode === "off") return <>{fallback}</>;
  if (m.path) return <Enlargeable key={m.path} src={imageUrl(m.path)} alt={alt} className={className} />;
  const { picture } = m;
  const failed = picture.error ? (picture.error instanceof ApiError && picture.error.code === "no_image_model" ? null : errorMessage(picture.error)) : null;
  return (
    <>
      {fallback}
      {picture.isFetching ? (
        <p className="sub" style={{ margin: 0, fontSize: 13 }} role="status">
          Drawing on mannequin…
        </p>
      ) : (
        <>
          {failed && (
            <p className="err" role="alert">
              {failed}
            </p>
          )}
          {(m.mode === "button" || picture.error) && (
            <button type="button" className="btn sm" style={{ justifySelf: "start", alignSelf: "flex-start" }} onClick={() => (picture.error ? picture.refetch() : m.request())}>
              {picture.error ? "Try the mannequin again" : "Show on mannequin"}
            </button>
          )}
        </>
      )}
    </>
  );
}

/** A picture that opens full size on a tap; a tap anywhere or Escape closes it (the native dialog handles Escape). */
function Enlargeable({ src, alt, className }: { src: string; alt: string; className: string }) {
  const big = useRef<HTMLDialogElement>(null);
  return (
    <>
      <img
        src={src}
        alt={alt}
        className={`${className} zoomable`}
        role="button"
        tabIndex={0}
        aria-label={`Enlarge: ${alt}`}
        onClick={() => big.current?.showModal()}
        onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), big.current?.showModal())}
      />
      <dialog ref={big} className="enlarged" aria-label={alt} onClick={() => big.current?.close()}>
        <img src={src} alt={alt} />
      </dialog>
    </>
  );
}

const DOTS = [0, 1, 2];

/** The chat under a mannequin picture: ask for one change in words and the picture is redrawn with it. */
export function MannequinChat({ m }: { m: Mannequin }) {
  const [ask, setAsk] = useState("");
  if (!m.path) return null;
  const busy = m.refine.isPending;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const text = ask.trim();
    if (!text || busy) return;
    m.refine.mutate(text, { onSuccess: () => setAsk("") });
  };
  return (
    <div className="mq-chat">
      {m.changes.length > 0 && (
        <ol className="mq-asks" aria-label="Changes made">
          {m.changes.map((c, i) => (
            <li key={i} className={i === m.changes.length - 1 ? "now" : undefined}>
              {c.ask}
            </li>
          ))}
        </ol>
      )}
      {busy ? (
        <div className="typing" role="status">
          {DOTS.map((k) => (
            <i key={k} style={{ "--k": k } as React.CSSProperties} />
          ))}
          <span>Redrawing…</span>
        </div>
      ) : (
        m.changes.length > 0 && (
          <button type="button" className="lnk mq-back" onClick={m.back}>
            Undo the last change
          </button>
        )
      )}
      {m.refine.error && (
        <p role="alert" className="err">
          {chatErrorText(m.refine.error)}
        </p>
      )}
      <form className="mq-ask" onSubmit={submit}>
        <input
          className="askin"
          value={ask}
          maxLength={300}
          placeholder={m.changes.length ? "Another change" : "Ask for a change"}
          aria-label="Ask for a change to the picture, such as tuck the shirt in or add a belt"
          title="Say what to change, such as “tuck the shirt in” or “add a belt”. Each change redraws the picture."
          autoComplete="off"
          disabled={busy}
          onChange={(e) => setAsk(e.target.value)}
        />
        <button type="submit" className="send" aria-label="Redraw with this change" disabled={busy || !ask.trim()}>
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M12 19V5M5 12l7-7 7 7" />
          </svg>
        </button>
      </form>
    </div>
  );
}
