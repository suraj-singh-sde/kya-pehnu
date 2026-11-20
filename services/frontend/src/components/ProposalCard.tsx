import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, unwrap, type ProposalView } from "@/api/client";
import { localToday } from "@/components/outfitHelpers";
import { ItemPic } from "@/components/wardrobe/GarmentIcons";
import { blockedReason, chatErrorText } from "@/lib/chat";

type Action = "confirm" | "cancel";
const STATE_LABEL: Record<ProposalView["state"], string> = {
  pending: "Waiting for you",
  confirmed: "Confirmed",
  cancelled: "Cancelled",
  expired: "Expired",
};
const GONE = "Item no longer available";
const REFRESH_AFTER_CONFIRM = ["outfits", "items", "item", "events"];

/** What pressing Confirm would do, in one line. */
function proposalTitle(p: ProposalView): string {
  if (p.kind === "log_wear") return "Log as worn today";
  const name = p.items[0]?.name ?? "an item that no longer exists";
  return `Mark ${name} as ${p.status_to} (now ${p.status_from})`;
}

/** A change the stylist proposed. Nothing happens until Confirm; the session view stays the only source of its state. */
export function ProposalCard({ proposal, sessionId }: { proposal: ProposalView; sessionId: number }) {
  const qc = useQueryClient();
  const decide = useMutation({
    mutationFn: (action: Action) =>
      unwrap(api.POST(`/api/chat/proposals/{tool_call_id}/${action}`, { params: { path: { tool_call_id: proposal.tool_call_id } } })),
    onSuccess: (_view, action) =>
      action === "confirm" ? Promise.all(REFRESH_AFTER_CONFIRM.map((k) => qc.invalidateQueries({ queryKey: [k] }))) : undefined,
    // Success or a 409 (expired, already decided): the stored state decides what the card shows next.
    onSettled: () => qc.invalidateQueries({ queryKey: ["chat-session", sessionId] }),
  });
  const title = proposalTitle(proposal);
  const pending = proposal.state === "pending";

  return (
    <article className="ask" aria-label={`Proposal: ${title}`}>
      <strong>
        {title} <em className="tag" style={{ fontWeight: 500 }}>{STATE_LABEL[proposal.state]}</em>
      </strong>
      <div className="strip">
        {proposal.items.map((item, i) => (
          <figure key={proposal.item_ids[i]}>
            {item ? <ItemPic item={item} /> : <div className="pic" role="img" aria-label={GONE} />}
            <figcaption>{item?.name ?? GONE}</figcaption>
          </figure>
        ))}
      </div>
      <span>Proposed on {proposal.proposed_on}</span>
      {pending && proposal.actionable && (
        <div className="row">
          <button className="btn primary sm" aria-label={`Confirm: ${title}`} disabled={decide.isPending} onClick={() => decide.mutate("confirm")}>
            {decide.isPending && decide.variables === "confirm" ? "Confirming…" : "Confirm"}
          </button>
          <button className="btn sm" aria-label={`Cancel: ${title}`} disabled={decide.isPending} onClick={() => decide.mutate("cancel")}>
            Not now
          </button>
        </div>
      )}
      {pending && !proposal.actionable && (
        <div className="row">
          <button className="btn primary sm" aria-label={`Confirm: ${title}`} disabled aria-describedby={`why-${proposal.tool_call_id}`}>
            Confirm
          </button>
          <span id={`why-${proposal.tool_call_id}`}>{blockedReason(proposal.proposed_on, localToday())}</span>
        </div>
      )}
      {decide.error && (
        <p role="alert" className="err">
          {chatErrorText(decide.error)}
        </p>
      )}
      <div role="status">{proposal.result && <p className="okline">{proposal.result}</p>}</div>
    </article>
  );
}
