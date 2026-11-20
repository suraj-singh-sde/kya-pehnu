import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, errorMessage, unwrap } from "@/api/client";
import { LlmForm } from "@/components/LlmForm";
import { Button } from "@/components/ui/button";
import { skipSetup } from "@/lib/setup";

export default function Setup() {
  const navigate = useNavigate();
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => unwrap(api.GET("/api/settings")) });
  if (settings.isLoading) return <p className="text-sm text-muted-foreground">Loading…</p>;
  if (settings.error || !settings.data) return <p className="text-sm text-destructive">{errorMessage(settings.error)}</p>;

  const canEdit = settings.data.llm.can_edit;
  return (
    <div className="max-w-2xl space-y-4">
      <h1 className="text-xl font-semibold">Welcome to Kya Pehnu</h1>
      {canEdit ? (
        <>
          <p className="text-sm text-muted-foreground">
            Kya Pehnu uses an AI model to look at your clothing photos and describe each piece. Choose which one to use.
            Your photos are sent to the provider you pick; Ollama keeps everything on this computer.
          </p>
          <LlmForm llm={settings.data.llm} onSaved={() => navigate("/add")} />
        </>
      ) : (
        <p className="text-sm text-muted-foreground">
          Kya Pehnu uses an AI model to look at your clothing photos and describe each piece. Only the admin of this
          Kya Pehnu chooses which one, so there is nothing to set up here.
        </p>
      )}
      <Button
        variant="ghost"
        size="sm"
        onClick={() => {
          skipSetup();
          navigate("/");
        }}
      >
        {canEdit ? "Skip for now" : "Continue"}
      </Button>
    </div>
  );
}
