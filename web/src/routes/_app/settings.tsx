import { createFileRoute } from "@tanstack/react-router";
import { Alert } from "#/components/arc/alert/alert";
import { Button } from "#/components/arc/button/button";
import { Input } from "#/components/arc/input/input";
import { Field, Heading, Panel, State, Status, field } from "#/features/workspace/ui";
import { setSession, useAction, useResource } from "#/lib/api";
import { useEffect, useState, type FormEvent } from "react";

type Settings = {
  agent_models: Record<string, { provider: string; model_id: string }> | null;
  max_concurrency: number;
  test_case_style: string;
};
const roles = ["orchestrator", "analyst", "planner", "evaluator"] as const;
export const Route = createFileRoute("/_app/settings")({ component: SettingsPage });

function SettingsPage() {
  const { data, loading, error, reload } = useResource<{
    settings: Settings;
    settings_id: string;
    credentials: Record<string, boolean>;
    workspace_root: string;
    account: { display_name: string; default_password: boolean };
  }>("/api/settings");
  const action = useAction();
  const [saved, setSaved] = useState(false);
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaved(false);
    const form = new FormData(event.currentTarget);
    const agent_models = Object.fromEntries(
      roles.map((role) => [
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
      setSaved(true);
      await reload();
    } catch {
      /* Error is displayed next to the form. */
    }
  }
  return (
    <div className="space-y-5">
      <Heading
        eyebrow="Configuration"
        title="Settings"
        description="Manage your account, workspace location, and models."
      />
      <State loading={loading} error={error} />
      {data && (
        <>
          <AccountSettings account={data.account} />
          <WorkspaceSettings path={data.workspace_root} onSaved={reload} />
          <Alert
            title={data.settings.agent_models ? "New workflows only" : "Models not configured"}
            tone={data.settings.agent_models ? "info" : "warning"}
          >
            {data.settings.agent_models
              ? "Existing workflows keep their original pinned model snapshot. Saving does not contact a provider."
              : "Model setup was skipped. Choose a provider and model for every agent before creating workflows or running benchmarks. Saving does not contact a provider."}
          </Alert>
          <Panel title="Models & defaults">
            <p className="text-sm text-muted-foreground">
              One explicit model per agent. No automatic routing or fallback.
            </p>
            <form
              key={data.settings_id}
              onSubmit={(event) => void save(event)}
              className="space-y-4"
            >
              {roles.map((role) => (
                <div
                  key={role}
                  className="grid gap-3 border-b border-border pb-4 md:grid-cols-[1fr_10rem_1fr]"
                >
                  <strong className="capitalize">{role}</strong>
                  <Field label="Provider">
                    <select
                      name={`${role}_provider`}
                      className={field}
                      defaultValue={data.settings.agent_models?.[role]?.provider ?? ""}
                      required
                    >
                      <option value="" disabled>
                        Select provider
                      </option>
                      <option>openai</option>
                      <option>anthropic</option>
                      <option>google</option>
                    </select>
                  </Field>
                  <Field label="Model ID">
                    <input
                      name={`${role}_model_id`}
                      defaultValue={data.settings.agent_models?.[role]?.model_id ?? ""}
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
              {saved && <Alert tone="success" title="Model settings saved" />}
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

function WorkspaceSettings({ path, onSaved }: { path: string; onSaved: () => Promise<void> }) {
  const action = useAction<{ workspace_root: string }>();
  const [workspacePath, setWorkspacePath] = useState(path);
  const [savedPath, setSavedPath] = useState("");
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSavedPath("");
    const form = new FormData(event.currentTarget);
    try {
      const result = await action.send("/api/workspace", { path: form.get("path") });
      setWorkspacePath(result.workspace_root);
      setSavedPath(result.workspace_root);
      await onSaved();
    } catch {
      /* Error is displayed next to this form. */
    }
  }
  return (
    <Panel title="Workspace location">
      <p className="text-sm text-muted-foreground">
        The default is ~/.quality-platform/workspace. Changes apply to new projects only; existing
        projects stay in their current locations and are not moved.
      </p>
      <p className="break-all text-xs text-muted-foreground">Current workspace: {path}</p>
      <form onSubmit={(event) => void save(event)} className="space-y-4">
        <Input
          label="Workspace path"
          name="path"
          value={workspacePath}
          onChange={(event) => setWorkspacePath(event.target.value)}
          placeholder="~/.quality-platform/workspace"
          required
        />
        {action.error && (
          <Alert tone="danger" title="Workspace location not saved">
            {action.error}
          </Alert>
        )}
        {savedPath && (
          <Alert tone="success" title="Workspace location saved">
            New projects will be created in {savedPath}. Existing projects have not moved.
          </Alert>
        )}
        <Button type="submit" size="sm" loading={action.busy}>
          Save workspace location
        </Button>
      </form>
    </Panel>
  );
}

function AccountSettings({
  account,
}: {
  account: { display_name: string; default_password: boolean };
}) {
  const action = useAction<{ logged_out: boolean }>();
  const [validationError, setValidationError] = useState("");
  const [saved, setSaved] = useState(() => sessionStorage.getItem("account-saved") === "true");
  useEffect(() => {
    sessionStorage.removeItem("account-saved");
  }, []);
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setValidationError("");
    setSaved(false);
    const form = new FormData(event.currentTarget);
    const newPassword = String(form.get("new_password") ?? "");
    if (newPassword !== form.get("confirm_password")) {
      setValidationError("The new passwords do not match.");
      return;
    }
    if (newPassword && newPassword.length < 12) {
      setValidationError("The new password must be at least 12 characters.");
      return;
    }
    try {
      const result = await action.send("/api/account", {
        display_name: form.get("display_name"),
        current_password: form.get("current_password"),
        ...(newPassword ? { new_password: newPassword } : {}),
      });
      if (result.logged_out) {
        setSession(null);
        sessionStorage.setItem("account-password-changed", "true");
        window.location.assign("/login");
      } else {
        sessionStorage.setItem("account-saved", "true");
        window.location.assign("/settings");
      }
    } catch {
      /* Error is displayed next to this form. */
    }
  }
  return (
    <Panel title="Account">
      {account.default_password && (
        <Alert tone="warning" title="Initial password still in use">
          Replace the initial password with one of at least 12 characters. This warning stays until
          you replace it.
        </Alert>
      )}
      <form onSubmit={(event) => void save(event)} className="space-y-4">
        <Input
          label="Display name"
          name="display_name"
          autoComplete="nickname"
          defaultValue={account.display_name}
          required
        />
        <Input
          label="Current password"
          name="current_password"
          type="password"
          autoComplete="current-password"
          description="Required to change your display name or password."
          required
        />
        <Input
          label="New password"
          name="new_password"
          type="password"
          autoComplete="new-password"
          minLength={12}
          description="At least 12 characters. Leave blank to keep your current password."
        />
        <Input
          label="Confirm new password"
          name="confirm_password"
          type="password"
          autoComplete="new-password"
          minLength={12}
        />
        <p className="text-sm text-muted-foreground">
          Changing your password signs out every session, including this one. Sign in again with
          your new password.
        </p>
        {(validationError || action.error) && (
          <Alert tone="danger" title="Account not saved">
            {validationError || action.error}
          </Alert>
        )}
        {saved && <Alert tone="success" title="Account name saved" />}
        <Button type="submit" size="sm" loading={action.busy}>
          Save account
        </Button>
      </form>
    </Panel>
  );
}
