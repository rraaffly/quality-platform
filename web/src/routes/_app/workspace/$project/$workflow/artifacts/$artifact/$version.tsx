import { Link, createFileRoute } from "@tanstack/react-router";
import { Alert } from "#/components/arc/alert/alert";
import { ActionForm, Field, Heading, Panel, State, Status, field } from "#/features/workspace/ui";
import { type Workflow, resourcePath, useResource } from "#/lib/api";

type Artifact = {
  item: Workflow["artifacts"][number];
  content: Record<string, string>;
  view: Workflow;
  rejected_by: string | null;
};
export const Route = createFileRoute(
  "/_app/workspace/$project/$workflow/artifacts/$artifact/$version",
)({ component: ArtifactPage });

function ArtifactPage() {
  const { project, workflow, artifact, version } = Route.useParams();
  const path = `/api/projects/${resourcePath(project)}/workflows/${resourcePath(workflow)}/artifacts/${resourcePath(artifact)}/versions/${resourcePath(version)}`;
  const { data, loading, error } = useResource<Artifact>(path);
  const item = data?.item;
  const latest =
    data?.view.artifacts.find((entry) => entry.id === artifact)?.version === item?.version;
  const base = {
    project_id: project,
    workflow_id: workflow,
    target_id: artifact,
    state_hash: data?.view.state_hash,
  };
  return (
    <div className="space-y-5">
      <Link
        to="/workspace/$project/$workflow"
        params={{ project, workflow }}
        hash="artifacts"
        className="text-xs text-primary hover:underline"
      >
        ← {workflow} / Artifacts
      </Link>
      <Heading
        eyebrow={`${item?.kind ?? "Artifact"} · v${version}`}
        title={artifact}
        description={
          item && `Scope ${item.scope_id} · Review only the version and evidence shown here.`
        }
        action={item && <Status value={item.status} />}
      />
      <State loading={loading} error={error} />
      {data && item && (
        <>
          {item.staleness !== "CURRENT" && (
            <Alert tone="warning" title={item.staleness}>
              Requirements changed. Review their impact before approval.
            </Alert>
          )}
          <Panel title="Version integrity">
            <p className="break-all text-xs">
              Content hash: <code>{item.content_hash}</code>
            </p>
            <p className="break-all text-xs">
              Requirement baseline: <code>{item.requirement_baseline_id}</code>
            </p>
            <div className="flex flex-wrap gap-3">
              {Object.keys(data.content).map((format) => (
                <a
                  key={format}
                  className="text-sm text-primary underline"
                  href={`${path}/download?format=${encodeURIComponent(format)}`}
                >
                  Download {format} ↓
                </a>
              ))}
            </div>
          </Panel>
          <Panel title="Artifact content">
            {Object.entries(data.content).map(([format, text], index) => (
              <details
                key={format}
                open={index === 0}
                className="min-w-0 border-b border-border pb-3"
              >
                <summary className="cursor-pointer text-sm font-medium">{format}</summary>
                <pre className="mt-3 max-h-[40rem] overflow-auto whitespace-pre-wrap break-words text-xs">
                  {text}
                </pre>
              </details>
            ))}
          </Panel>
          {latest ? (
            <Panel title="Human review">
              {data.rejected_by ? (
                <Alert tone="warning" title="Review rejected">
                  {data.rejected_by} rejected this version. Prepare a new revision before reviewing
                  again.
                </Alert>
              ) : item.status === "DRAFT" ? (
                <ActionForm
                  values={{ ...base, kind: "submit" }}
                  label="Submit for review"
                  review
                  variant="secondary"
                >
                  <Field label="Approved baseline for this scope">
                    <select name="baseline_id" className={field} required defaultValue="">
                      <option value="">Select baseline</option>
                      {data.view.baselines
                        .filter((entry) => entry.scope_id === item.scope_id)
                        .map((entry) => (
                          <option key={entry.id} value={entry.id}>
                            {entry.id.slice(0, 12)} · {entry.actor}
                          </option>
                        ))}
                    </select>
                  </Field>
                </ActionForm>
              ) : item.status === "READY_FOR_REVIEW" ? (
                <div className="flex flex-wrap gap-4">
                  <ActionForm
                    values={{ ...base, kind: "decide", action: "APPROVE" }}
                    label={`Approve v${item.version}`}
                    review
                  />
                  <ActionForm
                    values={{ ...base, kind: "decide", action: "REJECT" }}
                    label="Reject version"
                    variant="secondary"
                    review
                  />
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">
                  This version is approved. Later versions do not overwrite its historical decision.
                </p>
              )}
            </Panel>
          ) : (
            <Alert title="Historical version" tone="info">
              Open the latest version from the workflow to make a new decision.
            </Alert>
          )}
          {latest && item.kind === "TEST_CASES" && (
            <Panel title="Targeted revision">
              <ActionForm
                values={{
                  project_id: project,
                  workflow_id: workflow,
                  kind: "revision",
                  target_id: artifact,
                }}
                label="Create diff for review"
                cost
                variant="secondary"
              >
                <Field label="One test case instruction">
                  <textarea
                    className={field}
                    name="instruction"
                    required
                    rows={3}
                    placeholder="Change TC-012 priority to HIGH without changing other cases."
                  />
                </Field>
              </ActionForm>
            </Panel>
          )}
          <Panel title="Version history">
            {data.view.history
              .filter((event) => event.type === "artifact" && event.target === artifact)
              .map((event) => (
                <Link
                  key={event.seq}
                  to="/workspace/$project/$workflow/artifacts/$artifact/$version"
                  params={{ project, workflow, artifact, version: String(event.data.version) }}
                  className="block text-sm text-primary underline"
                >
                  v{String(event.data.version)} · {String(event.data.status)}
                </Link>
              ))}
          </Panel>
        </>
      )}
    </div>
  );
}
