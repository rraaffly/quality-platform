import { Link, createFileRoute } from "@tanstack/react-router";
import { Heading, Panel, State, Status } from "#/features/workspace/ui";
import { type Job, useResource } from "#/lib/api";

export const Route = createFileRoute("/_app/activity/")({ component: ActivityPage });

function ActivityPage() {
  const { data, loading, error } = useResource<{ jobs: Job[]; worker_healthy: boolean }>(
    "/api/jobs",
    true,
  );
  return (
    <div className="space-y-5">
      <Heading
        eyebrow="Execution"
        title="Activity"
        description="Queued commands, results and decisions that need review."
      />
      <State loading={loading} error={error} />
      {data && (
        <Panel title="Recent jobs">
          {data.jobs.length ? (
            data.jobs.map((job) => (
              <Link
                key={job.id}
                to="/activity/$job"
                params={{ job: job.id }}
                className="flex items-center justify-between gap-3 border-b border-border py-3 hover:text-primary"
              >
                <span>
                  <strong className="block text-sm">{job.command.kind.replaceAll("_", " ")}</strong>
                  <small className="text-muted-foreground">
                    {job.command.project_id} · {job.command.workflow_id || "project"} ·{" "}
                    {new Date(job.created * 1000).toLocaleString()}
                  </small>
                </span>
                <Status value={job.status} />
              </Link>
            ))
          ) : (
            <p className="text-sm text-muted-foreground">No commands yet.</p>
          )}
        </Panel>
      )}
    </div>
  );
}
