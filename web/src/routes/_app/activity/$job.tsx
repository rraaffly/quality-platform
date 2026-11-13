import { Link, createFileRoute } from "@tanstack/react-router";
import { Alert } from "#/components/arc/alert/alert";
import { ActionForm, Heading, Panel, State, Status } from "#/features/workspace/ui";
import { type Job, resourcePath, useResource } from "#/lib/api";

export const Route = createFileRoute("/_app/activity/$job")({ component: JobPage });

function JobPage() {
  const { job: identity } = Route.useParams();
  const {
    data: job,
    loading,
    error,
  } = useResource<Job>(`/api/jobs/${resourcePath(identity)}/review`, true);
  const command = job?.command;
  return (
    <div className="space-y-5">
      <Link to="/activity" className="text-xs text-primary hover:underline">
        ← Activity
      </Link>
      <Heading
        eyebrow="Execution"
        title={command?.kind.replaceAll("_", " ") ?? "Job"}
        description={identity}
        action={job && <Status value={job.status} />}
      />
      <State loading={loading} error={error} />
      {job && command && (
        <Panel title="Job status">
          {!job.worker_healthy && (
            <Alert tone="warning" title="Worker offline">
              Active execution cannot be confirmed. Review the result before retrying.
            </Alert>
          )}
          {job.error && (
            <Alert tone="danger" title="Job failed">
              {job.error}
            </Alert>
          )}
          {["FAILED", "INTERRUPTED"].includes(job.status) && (
            <ActionForm
              path={`/api/jobs/${resourcePath(identity)}/retry`}
              values={{}}
              label="Retry the same command"
              cost
              variant="secondary"
            />
          )}
          {["run", "resume"].includes(command.kind) && job.status === "BLOCKED" && (
            <>
              <p className="text-sm text-muted-foreground">
                Review prerequisites and already published output. Resume uses the original request
                and model snapshot.
              </p>
              <ActionForm
                values={{
                  kind: "resume",
                  project_id: command.project_id,
                  workflow_id: command.workflow_id,
                  request_id: command.request_id,
                }}
                label="Resume the same request"
                cost
                variant="secondary"
              />
            </>
          )}
          {command.workflow_id ? (
            <Link
              to="/workspace/$project/$workflow"
              params={{ project: command.project_id, workflow: command.workflow_id }}
              className="block text-sm text-primary underline"
            >
              Open workflow ↗
            </Link>
          ) : (
            <Link to="/workspace" className="block text-sm text-primary underline">
              Back to projects ↗
            </Link>
          )}
          {job.result &&
            (command.kind === "revision" ? (
              <>
                <h3 className="font-semibold">Review targeted diff</h3>
                <p className="text-xs text-muted-foreground">
                  Applying this diff creates a draft. Final approval remains separate.
                </p>
                <pre className="max-h-[32rem] overflow-auto border border-border bg-muted p-3 text-xs">
                  {job.diff}
                </pre>
                <code className="block break-all text-xs">
                  Diff hash: {String(job.result.diff_hash)}
                </code>
                <ActionForm
                  values={{
                    kind: "apply",
                    project_id: command.project_id,
                    workflow_id: command.workflow_id,
                    state_hash: job.current_hash,
                    draft_id: job.id,
                    reviewed_diff_hash: job.result.diff_hash,
                  }}
                  label="Apply reviewed diff"
                  review
                />
              </>
            ) : (
              <>
                <h3 className="font-semibold">
                  {command.kind === "scope_proposal"
                    ? "Proposal only — not applied"
                    : "Command result"}
                </h3>
                <pre className="max-h-[32rem] overflow-auto whitespace-pre-wrap break-words border border-border bg-muted p-3 text-xs">
                  {JSON.stringify(job.result, null, 2)}
                </pre>
              </>
            ))}
        </Panel>
      )}
    </div>
  );
}
