import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ThumbsDown, ThumbsUp } from "lucide-react";
import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { api, unwrap } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { chatErrorText, FEEDBACK_REASON_MAX, shownRating } from "@/lib/chat";

type Rating = -1 | 1;
type Props = {
  /** Rate by the outfit's item ids (cards) or by a saved outfit's id; exactly one is used, the outfit id first. */
  itemIds?: number[];
  outfitId?: number | null;
  rating?: Rating | null;
  disabled?: boolean;
};

const OPTIONS: { value: Rating; label: string; Icon: typeof ThumbsUp }[] = [
  { value: 1, label: "Thumbs up", Icon: ThumbsUp },
  { value: -1, label: "Thumbs down", Icon: ThumbsDown },
];
const REFRESH = ["outfits", "chat-session", "item"];

/** Thumbs up or down on an outfit, with an optional reason. */
export function Thumbs({ itemIds, outfitId, rating, disabled = false }: Props) {
  const qc = useQueryClient();
  const reasonId = useId();
  const [choice, setChoice] = useState<Rating | null>(null);
  const [reason, setReason] = useState("");
  const [saved, setSaved] = useState<{ seen: number | null; value: number } | null>(null);
  const save = useMutation({
    mutationFn: (v: { rating: Rating; reason: string }) =>
      unwrap(api.POST("/api/feedback", { body: { ...v, ...(outfitId != null ? { outfit_id: outfitId } : { item_ids: itemIds }) } })),
    onSuccess: () => Promise.all(REFRESH.map((k) => qc.invalidateQueries({ queryKey: [k] }))),
  });

  const shown = shownRating(rating, saved);
  const thumbs = useRef<Partial<Record<Rating, HTMLButtonElement | null>>>({});
  const returnTo = useRef<Rating | null>(null);
  const close = () => {
    returnTo.current = choice;
    setChoice(null);
    setReason("");
  };
  // The form is gone and the thumbs are enabled again: put the keyboard focus back on the thumb that opened it.
  useEffect(() => {
    if (choice == null && !save.isPending && returnTo.current != null) {
      thumbs.current[returnTo.current]?.focus();
      returnTo.current = null;
    }
  });
  const open = (value: Rating) => {
    save.reset();
    setChoice(value);
  };
  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (choice == null) return;
    const value = choice;
    save.mutate(
      { rating: value, reason: reason.trim() },
      {
        onSuccess: () => {
          setSaved({ seen: rating ?? null, value });
          close();
        },
      },
    );
  };

  return (
    <div className="space-y-1">
      <div role="group" aria-label="Rate this outfit" className="flex gap-1">
        {OPTIONS.map(({ value, label, Icon }) => (
          <Button
            key={value}
            ref={(el) => {
              thumbs.current[value] = el;
            }}
            type="button"
            variant={shown === value ? "default" : "outline"}
            size="icon-xs"
            aria-label={label}
            aria-pressed={shown === value}
            disabled={disabled || save.isPending}
            onClick={() => open(value)}
          >
            <Icon aria-hidden="true" />
          </Button>
        ))}
      </div>
      {choice != null && (
        <form className="flex flex-wrap items-center gap-1" onSubmit={onSubmit} onKeyDown={(e) => e.key === "Escape" && close()}>
          <label htmlFor={reasonId} className="sr-only">
            Reason (optional)
          </label>
          <Input
            id={reasonId}
            autoFocus
            maxLength={FEEDBACK_REASON_MAX}
            placeholder="Reason (optional)"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            className="h-7 min-w-0 flex-1 basis-32"
          />
          <Button type="submit" size="xs" disabled={save.isPending}>
            {save.isPending ? "Saving…" : "Save"}
          </Button>
          <Button type="button" size="xs" variant="outline" disabled={save.isPending} onClick={close}>
            Cancel
          </Button>
        </form>
      )}
      {save.error && (
        <p role="alert" className="text-xs text-destructive">
          {chatErrorText(save.error)}
        </p>
      )}
    </div>
  );
}
