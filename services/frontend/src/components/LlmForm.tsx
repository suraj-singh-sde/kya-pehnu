import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, errorMessage, unwrap, type LlmView } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

const selectCls = "h-9 w-full rounded-md border bg-background px-2 text-sm";
const OTHER = "__other__";

/** Pick a provider, paste a key, pick a model. Saving is refused by the server unless the model can read a photo. */
export function LlmForm({ llm, onSaved }: { llm: LlmView; onSaved?: () => void }) {
  const qc = useQueryClient();
  const [providerId, setProviderId] = useState(llm.active_provider ?? llm.providers[0].id);
  const preset = llm.providers.find((p) => p.id === providerId) ?? llm.providers[0];
  const startModel = (p: typeof preset) => p.saved_model ?? p.default_model ?? "";
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState(startModel(preset));
  const [models, setModels] = useState<string[]>([]);
  const [typing, setTyping] = useState(false);
  const changed = () => {
    // A result describes the values it was run with; once they change it would mislead.
    test.reset();
    save.reset();
  };

  const draft = () => ({ provider: providerId, api_key: apiKey || null, base_url: baseUrl || null });
  const load = useMutation({
    mutationFn: () => unwrap(api.POST("/api/settings/llm/models", { body: draft() })),
    onSuccess: (r) => {
      const list = r.models ?? [];
      setModels(list);
      // A model the provider does not list is cleared, so the person picks one instead of saving a hidden name.
      if (r.ok && list.length && !list.includes(model.trim())) setModel("");
    },
  });
  const test = useMutation({
    mutationFn: () => unwrap(api.POST("/api/settings/llm/test", { body: { ...draft(), model } })),
  });
  const save = useMutation({
    mutationFn: () => unwrap(api.PUT("/api/settings/llm", { body: { ...draft(), model } })),
    onSuccess: async () => {
      setApiKey("");
      await qc.invalidateQueries({ queryKey: ["settings"] });
      onSaved?.();
    },
  });

  const pick = (id: string) => {
    const next = llm.providers.find((p) => p.id === id)!;
    setProviderId(id);
    setApiKey("");
    setBaseUrl("");
    setModel(startModel(next));
    setModels([]);
    setTyping(false);
    load.reset();
    test.reset();
    save.reset();
  };

  if (!llm.can_edit) {
    return (
      <div className="space-y-1 text-sm">
        <p>
          {llm.configured ? (
            <>
              Using <code>{llm.model}</code>
              {llm.key_last4 && <> · key ••••{llm.key_last4}</>}
            </>
          ) : (
            "No AI provider is set up yet."
          )}
        </p>
        <p className="text-muted-foreground">
          {llm.managed_by_env
            ? "These settings come from the .env file or environment variables. Remove them there to choose here."
            : "Ask the administrator of this Kya Pehnu to change this."}
        </p>
      </div>
    );
  }

  const keyReady = !preset.needs_key || !!apiKey || preset.has_saved_key;
  const addressReady = preset.id !== "custom" || !!baseUrl || llm.active_provider === "custom";
  const ready = keyReady && addressReady && !!model;
  const busy = load.isPending || test.isPending || save.isPending;
  const listed = models.includes(model);

  return (
    <div className="space-y-4 text-sm">
      {llm.configured && (
        <p>
          In use: <code>{llm.model}</code>
          {llm.key_last4 && <> · key ••••{llm.key_last4}</>}
        </p>
      )}
      <div className="grid gap-2 sm:grid-cols-3">
        {llm.providers.map((p) => (
          <button
            key={p.id}
            type="button"
            onClick={() => pick(p.id)}
            aria-pressed={p.id === providerId}
            className={cn("rounded-md border p-3 text-left hover:bg-muted", p.id === providerId && "border-primary bg-muted font-medium")}
          >
            {p.label}
            {p.id === llm.active_provider && <span className="block text-xs text-muted-foreground">in use</span>}
          </button>
        ))}
      </div>

      {preset.id === "custom" && (
        <div className="grid gap-1">
          <Label htmlFor="llm-address">Server address</Label>
          <Input id="llm-address" placeholder={llm.base_url ?? "http://localhost:1234/v1"} value={baseUrl} onChange={(e) => {
              changed();
              setBaseUrl(e.target.value);
            }}
          />
        </div>
      )}

      {(preset.needs_key || preset.id === "custom") && (
        <div className="grid gap-1">
          <Label htmlFor="llm-key">
            API key{" "}
            {preset.key_url && (
              <a className="underline" href={preset.key_url} target="_blank" rel="noreferrer">
                Get a key
              </a>
            )}
          </Label>
          <Input
            id="llm-key"
            type="password"
            autoComplete="off"
            placeholder={preset.has_saved_key ? "saved (leave empty to keep it)" : "paste your key"}
            value={apiKey}
            onChange={(e) => {
              changed();
              setApiKey(e.target.value);
            }}
          />
        </div>
      )}
      {preset.id === "ollama" && preset.key_url && (
        <p className="text-muted-foreground">
          Runs on this computer and needs no key.{" "}
          <a className="underline" href={preset.key_url} target="_blank" rel="noreferrer">
            Install Ollama
          </a>{" "}
          and download a model that supports images first.
        </p>
      )}

      {preset.id === "openai" && (
        <p className="text-muted-foreground">
          With OpenAI, Kya Pehnu also makes a clean picture of every garment you upload. Each one is a paid image generation.
          To make them only when you press the button, set <code>auto_clean_pictures: false</code> in config.yaml.
        </p>
      )}

      <div className="grid gap-1">
        <Label htmlFor="llm-model">Model (must support images)</Label>
        <div className="flex gap-2">
          {models.length > 0 && !typing ? (
            <select
              id="llm-model"
              className={selectCls}
              value={listed ? model : model ? OTHER : ""}
              onChange={(e) => {
                changed();
                if (e.target.value === OTHER) setTyping(true);
                else setModel(e.target.value);
              }}
            >
              <option value="" disabled>
                Choose a model…
              </option>
              {models.map((m) => (
                <option key={m}>{m}</option>
              ))}
              <option value={OTHER}>Other (type a name)…</option>
            </select>
          ) : (
            <Input
              id="llm-model"
              placeholder="model name"
              value={model}
              onChange={(e) => {
                changed();
                setModel(e.target.value);
              }}
            />
          )}
          <Button variant="outline" disabled={!keyReady || !addressReady || busy} onClick={() => { setTyping(false); load.mutate(); }}>
            {load.isPending ? "Loading…" : "Load models"}
          </Button>
        </div>
        {load.data && !load.data.ok && (
          <p className="text-destructive">Could not load the list. {load.data.error} You can type the model name instead.</p>
        )}
        {load.error && <p className="text-destructive">{errorMessage(load.error)}</p>}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Button variant="outline" disabled={!ready || busy} onClick={() => test.mutate()}>
          {test.isPending ? "Testing…" : "Test"}
        </Button>
        <Button disabled={!ready || busy} onClick={() => save.mutate()}>
          {save.isPending ? "Checking and saving…" : "Save"}
        </Button>
        {test.data && (
          <span className={test.data.ok ? "text-green-600" : "text-destructive"}>
            {test.data.ok ? `Works: this model can read photos (${test.data.latency_ms} ms)` : test.data.error}
          </span>
        )}
      </div>
      {test.error && <p className="text-destructive">{errorMessage(test.error)}</p>}
      {save.error && <p className="text-destructive">{errorMessage(save.error)}</p>}
      {save.isSuccess && (
        <p className="text-green-600">Saved. The first photo downloads a helper model (about 170 MB) once.</p>
      )}
    </div>
  );
}
