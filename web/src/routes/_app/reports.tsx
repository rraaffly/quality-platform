import { Link, createFileRoute } from "@tanstack/react-router";
import { type FormEvent, useRef, useState } from "react";
import { Button } from "#/components/arc/button/button";
import { Alert } from "#/components/arc/alert/alert";
import { Field, Heading, Panel, State, Status, field } from "#/features/workspace/ui";
import { EmptyState } from "#/components/arc/empty-state/empty-state";
import { api, type Project, useAction, useResource } from "#/lib/api";

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
  const benchmarkForm = useRef<HTMLFormElement>(null);
  const [quote, setQuote] = useState<{
    fingerprint: string;
    case_count: number;
    provider_calls: number;
    reservation_usd: number;
    maximum_usd: number;
  } | null>(null);
  const [quoteError, setQuoteError] = useState("");

  function benchmarkPayload(form: FormData) {
    const rates = Object.fromEntries(
      ["analyst", "planner", "evaluator"].map((role) => [
        role,
        {
          input_usd_per_million: Number(form.get(`${role}_input`)),
          output_usd_per_million: Number(form.get(`${role}_output`)),
        },
      ]),
    );
    return {
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
    };
  }

  function quoteFingerprint(payload: ReturnType<typeof benchmarkPayload>) {
    return JSON.stringify({ ...payload, confirm_cost: false });
  }

  async function calculateEstimate() {
    if (!benchmarkForm.current) return;
    const payload = benchmarkPayload(new FormData(benchmarkForm.current));
    const params = new URLSearchParams({
      tier: String(payload.tier),
      dataset_version: String(payload.dataset_version),
      agent_role: String(payload.agent_role),
      repeats: String(payload.repeats),
    });
    setQuote(null);
    setQuoteError("");
    try {
      const estimate = await api<Omit<NonNullable<typeof quote>, "fingerprint">>(
        `/api/evaluation/estimate?${params}`,
      );
      setQuote({ ...estimate, fingerprint: quoteFingerprint(payload) });
    } catch (cause) {
      setQuoteError((cause as Error).message);
    }
  }

  async function runBenchmark(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const payload = benchmarkPayload(new FormData(event.currentTarget));
    if (!quote || quote.fingerprint !== quoteFingerprint(payload)) {
      setQuoteError("Calculate the reservation again after changing benchmark inputs.");
      return;
    }
    if (payload.max_cost_usd < quote.reservation_usd || payload.max_cost_usd > quote.maximum_usd) {
      setQuoteError(
        "The confirmed cap must cover the quoted reservation and stay within the tier limit.",
      );
      return;
    }
    setQuoteError("");
    try {
      await run.send("/api/evaluation/jobs", payload);
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
    <div className="space-y-10">
      <Heading
        eyebrow="Evaluation"
        title="Benchmark reports"
        description="Run a confirmed benchmark or inspect existing reports. Reading a report never starts a model call."
      />
      <section aria-labelledby="benchmark-heading" className="space-y-5">
        <div className="flex flex-wrap items-baseline justify-between gap-3 border-b border-border pb-4">
          <h2 id="benchmark-heading" className="text-xl font-semibold tracking-tight">
            Run a benchmark
          </h2>
          <span className="font-mono text-[11px] tracking-widest text-muted-foreground uppercase">
            Live provider execution
          </span>
        </div>
        <State
          loading={projects.loading || settings.loading}
          error={projects.error || settings.error}
        />
        {projects.data && settings.data && (
          <form
            ref={benchmarkForm}
            onSubmit={(event) => void runBenchmark(event)}
            className="grid items-start gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(18rem,0.65fr)]"
          >
            <div className="min-w-0 space-y-6">
              <Panel title="01 / Configure the run">
                <p className="text-sm leading-relaxed text-muted-foreground">
                  Choose the project and dataset to evaluate with your saved provider settings.
                </p>
                <div className="grid gap-x-5 gap-y-5 sm:grid-cols-2">
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
                </div>
              </Panel>
              <Panel title="02 / Declare token pricing">
                <p className="text-sm leading-relaxed text-muted-foreground">
                  Enter your provider's rates for every role. Recorded cost uses these declared
                  prices, in USD per million tokens.
                </p>
                {(["analyst", "planner", "evaluator"] as const).map((role) => (
                  <div key={role} className="grid gap-4 border-t border-border pt-5 sm:grid-cols-2">
                    <h3 className="font-mono text-[11px] tracking-widest text-muted-foreground uppercase sm:col-span-2">
                      {role}
                    </h3>
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
              </Panel>
            </div>
            <Panel title="03 / Review & confirm">
              <p className="text-sm leading-relaxed text-muted-foreground">
                This run calls configured providers and may incur charges. Calculate a reservation
                before authorizing execution.
              </p>
              <dl className="divide-y divide-border border-y border-border text-sm">
                <div className="flex justify-between gap-4 py-3">
                  <dt className="text-muted-foreground">Tier 2 limit</dt>
                  <dd className="font-mono">USD 5.00</dd>
                </div>
                <div className="flex justify-between gap-4 py-3">
                  <dt className="text-muted-foreground">Tier 3 limit</dt>
                  <dd className="font-mono">USD 20.00</dd>
                </div>
              </dl>
              <p className="text-xs leading-relaxed text-muted-foreground">
                Benchmark limits do not cap workflow costs.
              </p>
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
              <Button type="button" variant="secondary" onClick={() => void calculateEstimate()}>
                Calculate cost reservation
              </Button>
              {quote && (
                <div aria-live="polite" className="space-y-4 border-y border-border py-5">
                  <p className="font-mono text-[11px] tracking-widest text-muted-foreground uppercase">
                    Calculated reservation
                  </p>
                  <p className="font-mono text-2xl tracking-tight">
                    USD {quote.reservation_usd.toFixed(2)}
                  </p>
                  <p className="text-sm leading-relaxed text-muted-foreground">
                    {quote.case_count} approved cases ×{" "}
                    {quote.provider_calls / (quote.case_count || 1)} calls per case across all
                    repeats.
                  </p>
                  <p className="text-xs text-muted-foreground">
                    Tier maximum: USD {quote.maximum_usd.toFixed(2)}. Recalculate after changing any
                    inputs.
                  </p>
                </div>
              )}
              {quoteError && (
                <Alert tone="danger" title="Cost estimate unavailable">
                  {quoteError}
                </Alert>
              )}
              <label className="flex items-start gap-3 border-t border-border pt-5 text-sm leading-relaxed">
                <input
                  className="mt-1 shrink-0 accent-foreground"
                  type="checkbox"
                  name="confirm_cost"
                  required
                />
                <span>I confirm this live provider run and its declared maximum cost.</span>
              </label>
              {run.error && (
                <Alert tone="danger" title="Benchmark not queued">
                  {run.error}
                </Alert>
              )}
              <Button type="submit" loading={run.busy}>
                Queue benchmark
              </Button>
            </Panel>
          </form>
        )}
      </section>
      <Panel title="Register an existing report summary">
        <p className="max-w-2xl text-sm leading-relaxed text-muted-foreground">
          Imported reports are for viewing only; release evidence requires a Web benchmark run.
        </p>
        <form
          onSubmit={(event) => void importReport(event)}
          className="grid items-end gap-5 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]"
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
      <section aria-labelledby="history-heading" className="space-y-5">
        <div className="flex flex-wrap items-baseline justify-between gap-3 border-b border-border pb-4">
          <div>
            <p className="mb-2 font-mono text-[11px] tracking-widest text-muted-foreground uppercase">
              Report archive
            </p>
            <h2 id="history-heading" className="text-xl font-semibold tracking-tight">
              Report history
            </h2>
          </div>
          <p className="text-sm text-muted-foreground">
            Read-only summaries and benchmark evidence
          </p>
        </div>
        <State loading={loading} error={error} />
        {data &&
          (data.reports.length ? (
            data.reports.map((entry) => (
              <Panel
                key={entry.id}
                title={entry.report.run_id}
                action={<Status value={entry.report.decision} />}
              >
                <dl className="grid gap-4 text-sm sm:grid-cols-3">
                  <div className="min-w-0">
                    <dt className="mb-1 font-mono text-[10px] tracking-widest text-muted-foreground uppercase">
                      Project
                    </dt>
                    <dd className="break-all font-mono text-xs">{entry.project_id}</dd>
                  </div>
                  <div className="min-w-0">
                    <dt className="mb-1 font-mono text-[10px] tracking-widest text-muted-foreground uppercase">
                      Dataset
                    </dt>
                    <dd className="break-all font-mono text-xs">{entry.report.dataset_version}</dd>
                  </div>
                  <div>
                    <dt className="mb-1 font-mono text-[10px] tracking-widest text-muted-foreground uppercase">
                      Source
                    </dt>
                    <dd>
                      {entry.report.source === "web" ? "Web benchmark" : "Imported · viewing only"}
                    </dd>
                  </div>
                </dl>
                {entry.report.source === "web" && entry.report.record_id && (
                  <Link
                    to="/evaluation/$project/$report"
                    params={{ project: entry.project_id, report: entry.report.record_id }}
                    className="inline-block text-sm font-medium underline underline-offset-4"
                  >
                    Review candidate and attach evidence
                  </Link>
                )}
                <details className="border-t border-border pt-4">
                  <summary className="cursor-pointer font-mono text-[11px] tracking-wider text-muted-foreground uppercase">
                    Inspect report JSON
                  </summary>
                  <pre className="mt-4 max-h-[36rem] overflow-auto whitespace-pre-wrap break-words border border-border bg-background p-4 font-mono text-xs leading-relaxed">
                    {JSON.stringify(entry.report, null, 2)}
                  </pre>
                </details>
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
      </section>
    </div>
  );
}
