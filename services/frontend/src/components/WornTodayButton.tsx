import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, errorMessage, unwrap } from "@/api/client";
import { Button } from "@/components/ui/button";

/** Logs today as a wear of one item. Render it with `key={itemId}` so "Logged" never carries over to another item. */
export function WornTodayButton({ itemId }: { itemId: number }) {
  const qc = useQueryClient();
  const log = useMutation({
    mutationFn: () => unwrap(api.POST("/api/items/{item_id}/worn-today", { params: { path: { item_id: itemId } } })),
    onSuccess: () =>
      Promise.all([qc.invalidateQueries({ queryKey: ["items"] }), qc.invalidateQueries({ queryKey: ["item", itemId] })]),
  });
  return (
    <>
      <Button variant="outline" size="sm" disabled={log.isPending || log.isSuccess} onClick={() => log.mutate()}>
        {log.isPending ? "Logging…" : log.isSuccess ? "Logged" : "Worn today"}
      </Button>
      {log.error && (
        <p role="alert" className="text-xs text-destructive">
          {errorMessage(log.error)}
        </p>
      )}
    </>
  );
}
