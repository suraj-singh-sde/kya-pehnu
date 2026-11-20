import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type DragEvent, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, errorMessage, errorText, imageUrl, signInUrl, unwrap, type Job, type Piece } from "@/api/client";
import { CleanPicture } from "@/components/CleanPicture";
import { TagForm } from "@/components/TagForm";
import { pickTags, type Tags } from "@/lib/tags";
import { blouseHint } from "@/lib/blouse";
import { FORMALITY, section } from "@/lib/wardrobe";

const POLL_MS = 2000;
// A retry re-runs one photo without creating an active job, so poll for as long as a model call can take.
const RETRY_POLL_MS = 120_000;
const isActive = (j: Job) => j.status === "queued" || j.status === "running";

/** `onFinished`: called once everything uploaded has been dealt with after at least one piece went in (the sheet closes). */
export default function AddItems({ onFinished }: { onFinished?: () => void } = {}) {
  const qc = useQueryClient();
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => unwrap(api.GET("/api/ingest/jobs")),
    refetchInterval: (q) => (q.state.data?.some(isActive) ? POLL_MS : false),
  });
  const [pollUntil, setPollUntil] = useState(0);
  const working = !!jobs.data?.some(isActive) || Date.now() < pollUntil;
  const pieces = useQuery({
    queryKey: ["pieces"],
    queryFn: () => unwrap(api.GET("/api/ingest/pieces")),
    refetchInterval: working ? POLL_MS : false,
  });
  // A new piece is not offered until its clean picture is there: with auto clean pictures on, every piece of a job
  // still running gets one (or keeps its crop when that fails, which the job's end reveals).
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => unwrap(api.GET("/api/settings")) });
  // The wardrobe, to tell a saree whose blouse is already in from one whose blouse is still to be photographed.
  const items = useQuery({ queryKey: ["items"], queryFn: () => unwrap(api.GET("/api/items")) });
  const activeJobs = new Set((jobs.data ?? []).filter(isActive).map((j) => j.id));
  const cleaning = (p: Piece) =>
    p.state === "pending_review" && !!settings.data?.clean_pictures && !!settings.data.auto_clean_pictures && !p.alt_thumb_path && activeJobs.has(p.job_id);
  // Photos still in the pipeline: one placeholder card each, so the page shows what is coming. A photo whose pieces
  // are listed and being cleaned is not done yet, but has its own cards, so it needs no placeholder.
  const inFlight = new Set((pieces.data ?? []).filter(cleaning).map((p) => p.photo_id)).size;
  const pending = Math.max(0, (jobs.data ?? []).filter(isActive).reduce((n, j) => n + Math.max(0, j.total - j.processed), 0) - inFlight);
  const wasWorking = useRef(false);
  useEffect(() => {
    // The last change of a job (a finished photo, a clean picture) can land between two polls: fetch once more.
    if (wasWorking.current && !working) void qc.invalidateQueries({ queryKey: ["pieces"] });
    wasWorking.current = working;
  }, [working, qc]);
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["jobs"] });
    void qc.invalidateQueries({ queryKey: ["pieces"] });
    void qc.invalidateQueries({ queryKey: ["items"] });
  };

  // Multipart upload: plain fetch is simpler than openapi-fetch's body serializer here.
  const upload = useMutation({
    mutationFn: async (files: File[]) => {
      const fd = new FormData();
      files.forEach((f) => fd.append("files", f));
      const r = await fetch("/api/ingest", { method: "POST", body: fd });
      if (r.status === 401) location.assign(signInUrl());
      if (!r.ok) throw new Error(errorText(await r.json().catch(() => null)) || r.statusText);
      return r.json() as Promise<Job>;
    },
    onSuccess: refresh,
  });
  const [dragging, setDragging] = useState(false);
  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    if (e.dataTransfer.files.length) upload.mutate([...e.dataTransfer.files]);
  };

  const [selected, setSelected] = useState<number[]>([]);
  const merge = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/ingest/pieces/{piece_id}/merge", {
          params: { path: { piece_id: selected[0] } },
          body: { other_piece_ids: selected.slice(1) },
        }),
      ),
    onSuccess: () => {
      setSelected([]);
      refresh();
    },
  });
  const toggleSelect = (id: number) => setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));

  // Each card's tags as the person left them, so "Add all" adds what they checked, not what the model guessed.
  const edits = useRef(new Map<number, Tags | null>());
  // Ready to add as they are: tagged, cleaned, and not a possible duplicate, which needs a decision of its own.
  const ready = (pieces.data ?? []).filter((p) => p.state === "pending_review" && p.proposed_tags && !p.dup_reason && !cleaning(p));
  const added = useRef(false);
  const addAll = useMutation({
    mutationFn: async () => {
      for (const p of ready) {
        const overrides = edits.current.has(p.id!) ? edits.current.get(p.id!) : pickTags(p.proposed_tags!);
        await unwrap(api.POST("/api/ingest/pieces/{piece_id}/accept", { params: { path: { piece_id: p.id! } }, body: { overrides } }));
        added.current = true;
      }
    },
    onSettled: refresh,
  });
  const left = (pieces.data?.length ?? 0) + pending;
  useEffect(() => {
    if (added.current && !working && pieces.isFetched && !pieces.isFetching && left === 0 && !upload.isPending) {
      added.current = false;
      onFinished?.();
    }
  }, [left, working, pieces.isFetched, pieces.isFetching, upload.isPending, onFinished]);

  return (
    <div>
      <header>
        <h1>Add items</h1>
        <p className="sub">Drop in photos. Kya Pehnu cuts out the background and fills in the tags for you to check.</p>
      </header>
      <label
        className={`drop${dragging ? " over" : ""}${upload.isPending ? " busy" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <rect x="3" y="6" width="18" height="14" rx="2" />
          <circle cx="12" cy="13" r="3.5" />
          <path d="M8 6l1.5-2h5L16 6" />
        </svg>
        <p>
          <strong>
            <span className="desk">Drop photos here, or click to choose</span>
            <span className="ph">Tap to add photos</span>
          </strong>
        </p>
        <p className="sub" style={{ margin: 0 }}>JPG, PNG or WebP (export HEIC as JPEG). One piece per photo works best.</p>
        <input type="file" multiple accept="image/jpeg,image/png,image/webp" className="sr-only" onChange={(e) => e.target.files?.length && upload.mutate([...e.target.files])} />
        {upload.isPending && (
          <span className="sub" role="status">
            Uploading…
          </span>
        )}
        {upload.error && <span className="err">{errorMessage(upload.error)}</span>}
      </label>
      <div className="queue">
        {jobs.data?.filter(isActive).map((j) => (
          <div key={j.id} className="job" style={{ gridTemplateColumns: "1fr" }}>
            <div className="jb">
              <strong>
                {j.source === "upload" ? "Uploaded photos" : "Folder import"} · {j.processed} of {j.total} done
                {j.failed ? ` · ${j.failed} failed` : ""}
              </strong>
              <p className="st" role="status">
                {j.status === "queued" ? "Waiting to start" : "Removing the background and reading the tags…"}
              </p>
              <div className="bar">
                <i style={{ width: `${j.total ? Math.max(4, (100 * j.processed) / j.total) : 4}%` }} />
              </div>
            </div>
          </div>
        ))}
      </div>

      <section className="queue" aria-labelledby="review">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2 id="review">Check ({pieces.data?.length ?? 0})</h2>
          <div className="row">
            {selected.length > 0 && (
              <button className="btn sm" disabled={selected.length < 2 || merge.isPending} onClick={() => merge.mutate()}>
                Merge selected ({selected.length})
              </button>
            )}
            {ready.length > 1 && (
              <button className="btn primary sm" disabled={addAll.isPending} onClick={() => addAll.mutate()}>
                {addAll.isPending ? "Adding…" : `Add all to wardrobe (${ready.length})`}
              </button>
            )}
          </div>
        </div>
        {merge.error && <p className="err">{errorMessage(merge.error)}</p>}
        {addAll.error && <p className="err">{errorMessage(addAll.error)}</p>}
        {pieces.data?.length === 0 && pending === 0 && <p className="sub" style={{ margin: 0 }}>Nothing to check.</p>}
        {Array.from({ length: pending }, (_, i) => (
          <div key={`pending-${i}`} className="skeleton" aria-hidden="true">
            <div className="ph" />
            <div>
              <div className="ln" />
              <div className="ln w2" />
              <div className="ln w3" />
            </div>
          </div>
        ))}
        {pieces.data?.map((p) => (
          <PieceCard
            key={p.id}
            piece={p}
            hint={blouseHint(p, pieces.data ?? [], items.data ?? [])}
            cleaning={cleaning(p)}
            selected={selected.includes(p.id!)}
            busy={addAll.isPending}
            onSelect={() => toggleSelect(p.id!)}
            onTags={(t) => edits.current.set(p.id!, t)}
            onAdded={() => (added.current = true)}
            onDone={refresh}
            onRetry={() => setPollUntil(Date.now() + RETRY_POLL_MS)}
          />
        ))}
      </section>
    </div>
  );
}

type PieceCardProps = {
  piece: Piece;
  hint: string | null;
  cleaning: boolean;
  selected: boolean;
  busy: boolean;
  onSelect: () => void;
  onTags: (tags: Tags | null) => void;
  onAdded: () => void;
  onDone: () => void;
  onRetry: () => void;
};

function PieceCard({ piece, hint, cleaning, selected, busy, onSelect, onTags, onAdded, onDone, onRetry }: PieceCardProps) {
  const id = piece.id!;
  const [tags, setTagsState] = useState<Tags | null>(piece.proposed_tags ? pickTags(piece.proposed_tags) : null);
  const setTags = (t: Tags | null) => {
    setTagsState(t);
    onTags(t);
  };
  const act = useMutation({
    mutationFn: async (action: "accept" | "discard" | "duplicate" | "retry"): Promise<unknown> => {
      const path = { params: { path: { piece_id: id } } };
      if (action === "accept") return unwrap(api.POST("/api/ingest/pieces/{piece_id}/accept", { ...path, body: { overrides: tags } }));
      if (action === "discard") return unwrap(api.POST("/api/ingest/pieces/{piece_id}/discard", path));
      if (action === "duplicate")
        return unwrap(
          api.POST("/api/ingest/pieces/{piece_id}/mark-duplicate", { ...path, body: { item_id: piece.duplicate_of_item_id! } }),
        );
      return unwrap(api.POST("/api/ingest/photos/{photo_id}/retry", { params: { path: { photo_id: piece.photo_id } } }));
    },
    onSuccess: (_data, action) => {
      if (action === "retry") onRetry();
      if (action === "accept" || action === "duplicate") onAdded();
      onDone();
    },
  });
  const dup = useQuery({
    queryKey: ["item", piece.duplicate_of_item_id],
    enabled: !!piece.duplicate_of_item_id,
    queryFn: () => unwrap(api.GET("/api/items/{item_id}", { params: { path: { item_id: piece.duplicate_of_item_id! } } })),
  });

  // Turned away before tagging: not clothing, or innerwear. The message says which; the only thing to do is remove it.
  if (piece.state === "rejected") {
    return (
      <div className="job rejected">
        <div className="photo">
          <img src={imageUrl(piece.thumb_path)} alt="" />
        </div>
        <div className="jb">
          <p className="notice">{piece.error}</p>
          <div className="row">
            <button className="btn sm" disabled={act.isPending || busy} onClick={() => act.mutate("discard")}>
              Remove
            </button>
          </div>
        </div>
      </div>
    );
  }
  if (piece.state === "error") {
    return (
      <div className="job">
        <div className="photo">
          <img src={imageUrl(piece.thumb_path)} alt="" />
        </div>
        <div className="jb">
          <p className="err">{piece.error}</p>
          <div className="row">
            <button className="btn primary sm" disabled={act.isPending || busy} onClick={() => act.mutate("retry")}>
              Retry tagging
            </button>
            <button className="btn sm" onClick={() => act.mutate("discard")}>
              Skip
            </button>
          </div>
        </div>
      </div>
    );
  }

  const chips = tags && (
    <p className="tags">
      {[section(tags.category).name, tags.colors?.[0]?.name, tags.fabric, FORMALITY[tags.formality]].filter(Boolean).map((t) => (
        <span key={t} className="tag">{t}</span>
      ))}
    </p>
  );
  if (cleaning) {
    return (
      <div className="job cleaning" aria-busy="true">
        <div className="photo">
          <img src={imageUrl(piece.thumb_path)} alt="" />
        </div>
        <div className="jb">
          {chips}
          <div className="typing" role="status">
            {[0, 1, 2].map((k) => (
              <i key={k} style={{ "--k": k } as React.CSSProperties} />
            ))}
            <span>Making a clean picture of it. You can check it in a moment.</span>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className={`job${selected ? " sel" : ""}`}>
      <div className="photo">
        <img src={imageUrl(piece.thumb_path)} alt="" />
      </div>
      <div className="jb">
        {chips}
        {hint && <p className="notice">{hint}</p>}
        {piece.dup_reason && (
          <div className="notice">
            <p>Possible duplicate: {piece.dup_reason}</p>
            {dup.data && (
              <Link to={`/items/${dup.data.item.id}`} className="lnk row" style={{ marginTop: 4 }}>
                <img src={imageUrl(dup.data.item.thumb_path)} alt="" className="h-10 w-10 rounded border object-contain" />
                {dup.data.item.name}
              </Link>
            )}
          </div>
        )}
        <details open={!!tags && !piece.dup_reason}>
          <summary className="st" style={{ cursor: "pointer" }}>Check the tags</summary>
          <div style={{ marginTop: 10 }}>
            <CleanPicture kind="piece" id={id} row={piece} onChange={onDone} />
            {tags && <TagForm value={tags} onChange={setTags} ai={piece.proposed_tags} />}
          </div>
        </details>
        {act.error && <p className="err">{errorMessage(act.error)}</p>}
        <div className="row">
          <button className="btn primary sm" disabled={act.isPending || busy} onClick={() => act.mutate("accept")}>
            Add to wardrobe
          </button>
          {piece.duplicate_of_item_id && (
            <button className="btn sm" disabled={act.isPending || busy} onClick={() => act.mutate("duplicate")}>
              It's a duplicate
            </button>
          )}
          <button className="btn sm ghost" disabled={act.isPending || busy} onClick={() => act.mutate("discard")}>
            Skip
          </button>
          <label className="st row" style={{ marginLeft: "auto" }}>
            <input type="checkbox" checked={selected} onChange={onSelect} /> select to merge (same photo)
          </label>
        </div>
      </div>
    </div>
  );
}
