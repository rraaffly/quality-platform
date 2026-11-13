import { Link, createFileRoute } from "@tanstack/react-router";
import { type FormEvent } from "react";
import { Button } from "#/components/arc/button/button";
import { Alert } from "#/components/arc/alert/alert";
import { Field, Heading, Panel, State, Status, field } from "#/features/workspace/ui";
import { EmptyState } from "#/components/arc/empty-state/empty-state";
import { type Project, useAction, useResource } from "#/lib/api";

type Report = {
  id: string;
  project_id: string;
  report: {
    run_id: string;
    decision: string;
    dataset_version: string;
    source?: string;
    record_id?: string;
    [key: string]: unknown;
  };
};
export const Route = createFileRoute("/_app/reports")({ component: ReportsPage });

function ReportsPage() {
  const { data, loading, error } = useResource<{ reports: Report[] }>("/api/reports");
  const projects = useResource<{ projects: Project[] }>("/api/projects");
  const settings = useResource<{ settings_id: string }>("/api/settings");
  const run = useAction();
  const upload = useAction();

  async function runBenchmark(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const rates = Object.fromEntries(
      ["analyst", "planner", "evaluator"].map((role) => [
        role,
        {
          input_usd_per_million: Number(form.get(`${role}_input`)),
          output_usd_per_million: Number(form.get(`${role}_output`)),
        },
      ]),
    );
    try {
      await run.send("/api/evaluation/jobs", {
        kind: "run_benchmark",
        project_id: form.get("project_id"),
        settings_id: settings.data?.settings_id,
        tier: Number(form.get("tier")),
        dataset_version: form.get("dataset_version"),
        agent_role: form.get("agent_role"),
        repeats: Number(form.get("repeats")),
        max_cost_usd: Number(form.get("max_cost_usd")),
        rates,
        confirm_cost: form.get("confirm_cost") === "on",
      });
    } catch {
      // The action displays the error in the form.
    }
  }

  async function importReport(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    form.set("kind", "register_report");
    try {
      await upload.send("/api/evaluation/jobs", form);
    } catch {
      // The action displays the error in the form.
    }
  }
  return (
    <div className="space-y-5">
      <Heading
        eyebrow="Evaluation"
        title="Benchmark reports"
        description="Run a confirmed benchmark or inspect existing reports. Reading a report never starts a model call."
      />
      <Panel title="Run a benchmark">
        <p className="mb-4 text-sm text-muted-foreground">
          This calls configured providers. Declare token prices and a maximum cost before queuing.
          Tier 2 allows up to USD 5; Tier 3 allows up to USD 20. These limits do not cap workflow
          costs.
        </p>
        <State
          loading={projects.loading || settings.loading}
          error={projects.error || settings.error}
        />
        {projects.data && settings.data && (
          <form onSubmit={(event) => void runBenchmark(event)} className="space-y-4">
            <div className="grid gap-4 md:grid-cols-3">
              <Field label="Project">
                <select className={field} name="project_id" required>
                  <option value="">Choose project</option>
                  {projects.data.projects.map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Tier">
                <select className={field} name="tier" defaultValue="2">
                  <option value="2">Tier 2</option>
                  <option value="3">Tier 3</option>
                </select>
              </Field>
              <Field label="Dataset version">
                <input className={field} name="dataset_version" defaultValue="v0.1" required />
              </Field>
              <Field label="Agent role for Tier 2">
                <select className={field} name="agent_role">
                  <option value="analyst">Analyst</option>
                  <option value="planner">Planner</option>
                </select>
              </Field>
              <Field label="Repeats (Tier 3)">
                <input
                  className={field}
                  name="repeats"
                  type="number"
                  min="1"
                  max="5"
                  defaultValue="1"
                  required
                />
              </Field>
              <Field label="Maximum cost (USD)">
                <input
                  className={field}
                  name="max_cost_usd"
                  type="number"
                  min="0.01"
                  max="20"
                  step="0.01"
                  defaultValue="5"
                  required
                />
              </Field>
            </div>
            {(["analyst", "planner", "evaluator"] as const).map((role) => (
              <div key={role} className="grid gap-3 border-t border-border pt-3 md:grid-cols-3">
                <strong className="capitalize">{role} rates</strong>
                <Field label="Input USD / million tokens">
                  <input
                    className={field}
                    name={`${role}_input`}
                    type="number"
                    min="0"
                    step="any"
                    required
                  />
                </Field>
                <Field label="Output USD / million tokens">
                  <input
                    className={field}
                    name={`${role}_output`}
                    type="number"
                    min="0"
                    step="any"
                    required
                  />
                </Field>
              </div>
            ))}
            <label className="flex gap-2 text-sm">
              <input type="checkbox" name="confirm_cost" required />I confirm this live provider run
              and its declared maximum cost.
            </label>
            {run.error && (
              <Alert tone="danger" title="Benchmark not queued">
                {run.error}
              </Alert>
            )}
            <Button type="submit" loading={run.busy}>
              Queue benchmark
            </Button>
          </form>
        )}
      </Panel>
      <Panel title="Register an existing report summary">
        <p className="mb-3 text-sm text-muted-foreground">
          Imported reports are for viewing only; release evidence requires a Web benchmark run.
        </p>
        <form
          onSubmit={(event) => void importReport(event)}
          className="flex flex-wrap items-end gap-3"
        >
          <Field label="Project">
            <select className={field} name="project_id" required>
              <option value="">Choose project</option>
              {projects.data?.projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="report.json">
            <input className={field} name="file" type="file" accept=".json" required />
          </Field>
          <Button type="submit" variant="secondary" loading={upload.busy}>
            Register report
          </Button>
        </form>
        {upload.error && (
          <Alert tone="danger" title="Report not registered">
            {upload.error}
          </Alert>
        )}
      </Panel>
      <State loading={loading} error={error} />
      {data &&
        (data.reports.length ? (
          data.reports.map((entry) => (
            <Panel
              key={entry.id}
              title={entry.report.run_id}
              action={<Status value={entry.report.decision} />}
            >
              <p className="text-sm text-muted-foreground">
                Project {entry.project_id} · Dataset {entry.report.dataset_version}
              </p>
              {entry.report.source === "web" && entry.report.record_id && (
                <Link
                  to="/evaluation/$project/$report"
                  params={{ project: entry.project_id, report: entry.report.record_id }}
                  className="text-sm text-primary underline"
                >
                  Review candidate and attach evidence
                </Link>
              )}
              <pre className="max-h-[36rem] overflow-auto whitespace-pre-wrap break-words text-xs">
                {JSON.stringify(entry.report, null, 2)}
              </pre>
            </Panel>
          ))
        ) : (
          <Panel>
            <EmptyState
              title="No registered reports"
              description="Run a benchmark or register an existing summary. An empty dashboard is not evidence of release readiness."
            />
          </Panel>
        ))}
    </div>
  );
}
