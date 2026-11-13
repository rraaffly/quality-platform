import { Link, createFileRoute } from "@tanstack/react-router";
import { useState, type FormEvent } from "react";
import { Alert } from "#/components/arc/alert/alert";
import { Button } from "#/components/arc/button/button";
import { ActionForm, Field, Heading, Panel, State, Status, field } from "#/features/workspace/ui";
import { type Workflow, resourcePath, useAction, useResource } from "#/lib/api";

export const Route = createFileRoute("/_app/workspace/$project/$workflow/")({
  component: WorkflowPage,
});

function WorkflowPage() {
  const { project, workflow } = Route.useParams();
  const { data, loading, error } = useResource<Workflow>(
    `/api/projects/${resourcePath(project)}/workflows/${resourcePath(workflow)}`,
  );
  const [requestId] = useState(() => `request-${crypto.randomUUID()}`);
  const upload = useAction();
  async function importSources(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      await upload.send(
        `/api/projects/${resourcePath(project)}/workflows/${resourcePath(workflow)}/sources`,
        new FormData(event.currentTarget),
      );
    } catch {
      /* Show the error while keeping the same upload key. */
    }
  }
  const base = { project_id: project, workflow_id: workflow };
  const reviewed = { ...base, state_hash: data?.state_hash };
  return (
    <div className="space-y-6">
      <Link to="/workspace" className="text-xs text-primary hover:underline">
        ← Projects / {project}
      </Link>
      <Heading
        eyebrow={`Workflow · ${workflow}`}
        title={data?.workflow.goal ?? workflow}
        description="Review sources and each QA decision before moving forward."
        action={data && <Status value={data.workflow.status} />}
      />
      <State loading={loading} error={error} />
      {data && (
        <>
          <nav
            className="flex flex-wrap gap-4 border-b border-border pb-3 text-xs text-primary"
            aria-label="Workflow sections"
          >
            {[
              "instruction",
              "requirements",
              "clarifications",
              "scopes",
              "artifacts",
              "history",
            ].map((name) => (
              <a key={name} href={`#${name}`} className="hover:underline">
                {name}
              </a>
            ))}
          </nav>
          <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_19rem]">
            <div className="min-w-0 space-y-5">
              <div id="instruction">
                <Panel title="What would you like to work on?">
                  <ActionForm
                    values={{ ...base, kind: "run", request_id: requestId }}
                    label="Run instruction"
                    cost
                  >
                    <Field label="Instruction">
                      <textarea
                        name="instruction"
                        className={field}
                        rows={3}
                        required
                        placeholder="Analyze the source documents and identify unclear rules."
                      />
                    </Field>
                    <Field label="Test case format">
                      <select name="style" className={field} defaultValue="both">
                        <option value="both">Detailed + Gherkin</option>
                        <option value="detailed">Detailed</option>
                        <option value="gherkin">Gherkin</option>
                      </select>
                    </Field>
                  </ActionForm>
                </Panel>
              </div>
              <div id="requirements" className="space-y-3">
                <h2 className="font-semibold">Requirements · {data.requirements.length}</h2>
                {!data.requirements.length && (
                  <Panel>
                    <p className="text-sm text-muted-foreground">
                      Import sources and request an analysis to see traceable requirements.
                    </p>
                  </Panel>
                )}
                {data.requirements.map((item) => (
                  <Panel
                    key={item.id}
                    title={`${item.id} · v${item.version}`}
                    action={<Status value={item.readiness} />}
                  >
                    <p className="text-sm">{item.statement}</p>
                    <div className="flex flex-wrap gap-2">
                      {item.source_references.map((ref, index) => (
                        <Link
                          key={index}
                          to="/workspace/$project/$workflow/sources/$source"
                          params={{ project, workflow, source: ref.document_id }}
                          hash={`line-${ref.start_line}`}
                          className="text-xs text-primary underline"
                        >
                          Evidence lines {ref.start_line}–{ref.end_line}
                        </Link>
                      ))}
                    </div>
                    {!!item.dependency_ids.length && (
                      <p className="text-xs text-muted-foreground">
                        Dependencies: {item.dependency_ids.join(", ")}
                      </p>
                    )}
                  </Panel>
                ))}
              </div>
              <div id="clarifications" className="space-y-3">
                <h2 className="font-semibold">Clarifications · {data.clarifications.length}</h2>
                {!data.clarifications.length && (
                  <Panel>
                    <p className="text-sm text-muted-foreground">
                      No questions yet. This does not mean all rules are clear.
                    </p>
                  </Panel>
                )}
                {data.clarifications.map((item) => (
                  <Panel
                    key={item.id}
                    title={item.question}
                    action={<Status value={item.status} />}
                  >
                    <p className="text-xs text-muted-foreground">
                      {item.id} · {item.issue_type}
                    </p>
                    {item.answers.map((answer, index) => (
                      <blockquote key={index} className="border-l-2 border-primary pl-3 text-sm">
                        {answer.text}
                        <small className="block text-muted-foreground">
                          {answer.author} · {answer.source} · recorded by {answer.recorded_by}
                        </small>
                      </blockquote>
                    ))}
                    {item.resolution && <p className="text-sm">Resolution: {item.resolution}</p>}
                    {item.status !== "RESOLVED" && (
                      <details>
                        <summary className="cursor-pointer text-sm text-primary">
                          Record a stakeholder answer
                        </summary>
                        <div className="pt-3">
                          <ActionForm
                            values={{ ...reviewed, kind: "answer", target_id: item.id }}
                            label="Save answer"
                          >
                            <Field label="Stakeholder">
                              <input className={field} name="author" required />
                            </Field>
                            <Field label="Source (meeting or decision)">
                              <input className={field} name="source" required />
                            </Field>
                            <Field label="Answer">
                              <textarea className={field} name="text" required rows={3} />
                            </Field>
                            <p className="text-xs text-muted-foreground">
                              An answer is recorded, not automatically resolved. Re-run analysis to
                              validate it.
                            </p>
                          </ActionForm>
                        </div>
                      </details>
                    )}
                  </Panel>
                ))}
              </div>
              <div id="scopes" className="space-y-3">
                <h2 className="font-semibold">Scopes & requirement baselines</h2>
                {!data.scopes.length && (
                  <Panel>
                    <p className="text-sm text-muted-foreground">
                      Scopes appear after analysis. Scope and baseline approvals are separate
                      decisions.
                    </p>
                  </Panel>
                )}
                {data.scopes.map((item) => (
                  <Panel key={item.id} title={item.journey} action={<Status value={item.status} />}>
                    <p className="text-xs text-muted-foreground">
                      {item.id} · Requirements: {item.requirement_ids.join(", ")}
                    </p>
                    <p className="text-xs text-muted-foreground">
                      Exclusions: {item.excluded_requirement_ids.join(", ") || "none"} ·
                      Dependencies: {item.dependency_scope_ids.join(", ") || "none"}
                    </p>
                    {item.status === "PROPOSED" && (
                      <>
                        <div className="flex flex-wrap gap-3">
                          <ActionForm
                            values={{
                              ...reviewed,
                              kind: "scope",
                              target_id: item.id,
                              action: "APPROVE",
                            }}
                            label="Approve scope"
                            review
                          />
                          <ActionForm
                            values={{
                              ...reviewed,
                              kind: "scope",
                              target_id: item.id,
                              action: "REJECT",
                            }}
                            label="Reject scope"
                            variant="secondary"
                            review
                          />
                        </div>
                        <details>
                          <summary className="cursor-pointer text-sm text-primary">
                            Split / merge scope
                          </summary>
                          <div className="pt-3">
                            <ActionForm
                              values={{ ...reviewed, kind: "scope", target_id: item.id }}
                              label="Submit scope decision"
                              review
                            >
                              <Field label="Decision">
                                <select name="action" className={field}>
                                  <option>SPLIT</option>
                                  <option>MERGE</option>
                                </select>
                              </Field>
                              <Field label="Other scope IDs (JSON array)">
                                <textarea
                                  name="merge_with_ids"
                                  defaultValue="[]"
                                  className={field}
                                  rows={2}
                                  onChange={(event) => event.currentTarget.setCustomValidity("")}
                                />
                              </Field>
                              <Field label="Replacement proposals (JSON array)">
                                <textarea
                                  name="replacements"
                                  defaultValue="[]"
                                  className={field}
                                  rows={4}
                                  onChange={(event) => event.currentTarget.setCustomValidity("")}
                                />
                              </Field>
                            </ActionForm>
                          </div>
                        </details>
                      </>
                    )}
                    {item.status === "APPROVED" && (
                      <ActionForm
                        values={{ ...reviewed, kind: "baseline", target_id: item.id }}
                        label="Approve requirement baseline"
                        review
                        variant="secondary"
                      />
                    )}
                    {data.baselines
                      .filter((baseline) => baseline.scope_id === item.id)
                      .map((baseline) => (
                        <div key={baseline.id} className="border-t border-border pt-2 text-xs">
                          Approved baseline · {baseline.id} · {baseline.actor}
                          <pre className="mt-1 overflow-auto text-muted-foreground">
                            {JSON.stringify(baseline.requirement_versions, null, 2)}
                          </pre>
                        </div>
                      ))}
                  </Panel>
                ))}
                <Panel title="Propose a scope change">
                  <ActionForm
                    values={{ ...base, kind: "scope_proposal" }}
                    label="Create proposal (not applied)"
                    cost
                  >
                    <Field label="Instruction">
                      <textarea className={field} name="instruction" required rows={2} />
                    </Field>
                  </ActionForm>
                </Panel>
              </div>
              <div id="artifacts" className="space-y-3">
                <h2 className="font-semibold">Artifacts · {data.artifacts.length}</h2>
                {!data.artifacts.length && (
                  <Panel>
                    <p className="text-sm text-muted-foreground">
                      Ask for a test plan or test cases after reviewing eligible scopes. A draft is
                      not an approval.
                    </p>
                  </Panel>
                )}
                {data.artifacts.map((item) => (
                  <Link
                    key={item.id}
                    to="/workspace/$project/$workflow/artifacts/$artifact/$version"
                    params={{ project, workflow, artifact: item.id, version: String(item.version) }}
                    className="flex items-center justify-between gap-4 border border-border bg-card p-5 hover:bg-muted"
                  >
                    <span>
                      <small className="text-primary">{item.kind}</small>
                      <strong className="block text-sm">{item.id}</strong>
                      <small className="text-muted-foreground">
                        Scope {item.scope_id} · v{item.version}
                      </small>
                    </span>
                    <span className="flex gap-1">
                      <Status value={item.status} />
                      {item.staleness !== "CURRENT" && <Status value={item.staleness} />}
                    </span>
                  </Link>
                ))}
              </div>
              <div id="history">
                <Panel title="History & impact">
                  {data.history.length ? (
                    [...data.history].reverse().map((event) => (
                      <details key={event.seq} className="border-b border-border pb-2">
                        <summary className="cursor-pointer text-sm">
                          #{event.seq} · {event.type} · {event.target}
                        </summary>
                        <pre className="overflow-auto pt-2 text-xs">
                          {JSON.stringify(event.data, null, 2)}
                        </pre>
                      </details>
                    ))
                  ) : (
                    <p className="text-sm text-muted-foreground">No workflow events yet.</p>
                  )}
                </Panel>
              </div>
            </div>
            <aside className="space-y-5">
              <Panel title="Source snapshots">
                <p className="text-xs text-muted-foreground">
                  Immutable evidence; editing the original file does not change these snapshots.
                </p>
                {data.sources.length ? (
                  data.sources.map((source) => (
                    <Link
                      key={source.id}
                      to="/workspace/$project/$workflow/sources/$source"
                      params={{ project, workflow, source: source.id }}
                      className="block border-b border-border pb-2 text-sm text-primary hover:underline"
                    >
                      {source.name}
                      <small className="block text-muted-foreground">
                        {source.lines} lines · {source.hash.slice(0, 8)}
                      </small>
                    </Link>
                  ))
                ) : (
                  <p className="text-sm text-muted-foreground">No sources yet.</p>
                )}
                <form onSubmit={(event) => void importSources(event)} className="space-y-3">
                  <Field label="Add Markdown files">
                    <input
                      name="files"
                      type="file"
                      multiple
                      required
                      accept=".md"
                      className={field}
                    />
                  </Field>
                  <p className="text-xs text-muted-foreground">UTF-8 · 5 MiB/file · 20 MiB/batch</p>
                  {upload.error && (
                    <Alert title="Upload failed" tone="danger">
                      {upload.error}
                    </Alert>
                  )}
                  <Button size="sm" variant="secondary" type="submit" loading={upload.busy}>
                    Import snapshots
                  </Button>
                </form>
              </Panel>
              <Panel title="Pinned model snapshot">
                {data.snapshot ? (
                  <>
                    {Object.entries(data.snapshot.agent_models).map(([role, model]) => (
                      <div key={role} className="flex justify-between gap-2 text-xs">
                        <span>{role}</span>
                        <span>
                          {model.provider} · {model.model_id}
                        </span>
                      </div>
                    ))}
                    <p className="text-xs text-muted-foreground">
                      Concurrency: {data.snapshot.max_concurrency} · Format:{" "}
                      {data.snapshot.test_case_style}
                    </p>
                  </>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    No model snapshot is available for this workflow.
                  </p>
                )}
                <p className="text-xs text-muted-foreground">
                  Usage costs are not measured; there is no workflow monetary cap.
                </p>
              </Panel>
            </aside>
          </div>
        </>
      )}
    </div>
  );
}
