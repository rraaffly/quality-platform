import { createFileRoute } from "@tanstack/react-router";
import { Alert } from "#/components/arc/alert/alert";
import { Button } from "#/components/arc/button/button";
import { Field, Heading, Panel, State, Status, field } from "#/features/workspace/ui";
import { useAction, useResource } from "#/lib/api";
import type { FormEvent } from "react";

type Settings = {
  agent_models: Record<string, { provider: string; model_id: string }>;
  max_concurrency: number;
  test_case_style: string;
};
export const Route = createFileRoute("/_app/settings")({ component: SettingsPage });

function SettingsPage() {
  const { data, loading, error, reload } = useResource<{
    settings: Settings;
    settings_id: string;
    credentials: Record<string, boolean>;
  }>("/api/settings");
  const action = useAction();
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const agent_models = Object.fromEntries(
      ["orchestrator", "analyst", "planner", "evaluator"].map((role) => [
        role,
        {
          provider: form.get(`${role}_provider`),
          model_id: form.get(`${role}_model_id`),
        },
      ]),
    );
    try {
      await action.send("/api/settings", {
        agent_models,
        max_concurrency: Number(form.get("max_concurrency")),
        test_case_style: form.get("test_case_style"),
      });
      await reload();
    } catch {
      /* Error is displayed next to the form. */
    }
  }
  return (
    <div className="space-y-5">
      <Heading
        eyebrow="Configuration"
        title="Model settings"
        description="One explicit model per agent. No automatic routing or fallback."
      />
      <State loading={loading} error={error} />
      {data && (
        <>
          <Alert title="New workflows only" tone="info">
            Existing workflows keep their original pinned model snapshot. Saving does not contact a
            provider.
          </Alert>
          <Panel title="Models & defaults">
            <form
              key={data.settings_id}
              onSubmit={(event) => void save(event)}
              className="space-y-4"
            >
              {Object.entries(data.settings.agent_models).map(([role, model]) => (
                <div
                  key={role}
                  className="grid gap-3 border-b border-border pb-4 md:grid-cols-[1fr_10rem_1fr]"
                >
                  <strong className="capitalize">{role}</strong>
                  <Field label="Provider">
                    <select
                      name={`${role}_provider`}
                      className={field}
                      defaultValue={model.provider}
                    >
                      <option>openai</option>
                      <option>anthropic</option>
                      <option>google</option>
                    </select>
                  </Field>
                  <Field label="Model ID">
                    <input
                      name={`${role}_model_id`}
                      defaultValue={model.model_id}
                      className={field}
                      required
                    />
                  </Field>
                </div>
              ))}
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Max concurrency">
                  <input
                    type="number"
                    min={1}
                    max={16}
                    required
                    name="max_concurrency"
                    defaultValue={data.settings.max_concurrency}
                    className={field}
                  />
                </Field>
                <Field label="Test case format">
                  <select
                    name="test_case_style"
                    defaultValue={data.settings.test_case_style}
                    className={field}
                  >
                    <option>both</option>
                    <option>detailed</option>
                    <option>gherkin</option>
                  </select>
                </Field>
              </div>
              {action.error && (
                <Alert tone="danger" title="Settings not saved">
                  {action.error}
                </Alert>
              )}
              <Button type="submit" size="sm" loading={action.busy}>
                Save a new settings version
              </Button>
            </form>
            <p className="break-all text-xs text-muted-foreground">
              Active version: {data.settings_id}
            </p>
          </Panel>
          <Panel title="Provider connections">
            <p className="text-sm text-muted-foreground">
              Keys are provided by the server environment, never displayed or stored in the browser.
            </p>
            <div className="flex flex-wrap gap-5">
              {Object.entries(data.credentials).map(([provider, configured]) => (
                <div key={provider} className="flex items-center gap-2 text-sm capitalize">
                  {provider}
                  <Status value={configured ? "Configured" : "Missing credential"} />
                </div>
              ))}
            </div>
            <p className="text-xs text-muted-foreground">
              Configured means the variable exists, not that the key is valid. Restart services to
              apply environment changes.
            </p>
          </Panel>
          <Panel title="Cost & limits">
            <p className="text-sm">
              Workflow cost is not measured and there is no hard monetary cap. Benchmark pricing and
              budgets remain CLI-only.
            </p>
          </Panel>
        </>
      )}
    </div>
  );
}
