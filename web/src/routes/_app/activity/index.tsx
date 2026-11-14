import { Link, createFileRoute } from "@tanstack/react-router";
import { Activity, ArrowUpRight } from "lucide-react";
import { EmptyState } from "#/components/arc/empty-state/empty-state";
import { Heading, Panel, State, Status } from "#/features/workspace/ui";
import { type Job, useResource } from "#/lib/api";

export const Route = createFileRoute("/_app/activity/")({ component: ActivityPage });

function ActivityPage() {
  const { data, loading, error } = useResource<{ jobs: Job[]; worker_healthy: boolean }>(
    "/api/jobs",
    true,
  );
  return (
    <div className="space-y-8">
      <Heading
        eyebrow="Execution"
        title="Activity"
        description="Queued commands, results and decisions that need review."
      />
      <State loading={loading} error={error} />
      {data && (
        <Panel
          title="Recent jobs"
          action={
            <span className="font-mono text-[11px] tracking-widest text-muted-foreground uppercase">
              Execution log
            </span>
          }
        >
          {data.jobs.length ? (
            <ul className="divide-y divide-border">
              {data.jobs.map((job) => (
                <li key={job.id}>
                  <Link
                    to="/activity/$job"
                    params={{ job: job.id }}
                    className="group grid min-w-0 gap-4 py-6 transition-colors hover:bg-muted/40 focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-foreground md:grid-cols-[minmax(0,1fr)_auto]"
                  >
                    <span className="min-w-0 space-y-3">
                      <span className="flex items-center gap-3">
                        <strong className="text-base font-medium capitalize">
                          {job.command.kind.replaceAll("_", " ")}
                        </strong>
                        <ArrowUpRight
                          aria-hidden="true"
                          className="size-4 shrink-0 text-muted-foreground group-hover:text-foreground"
                        />
                      </span>
                      <span className="block break-all font-mono text-[11px] text-muted-foreground">
                        {job.id}
                      </span>
                      <span className="grid gap-3 text-xs sm:grid-cols-2">
                        <span className="min-w-0">
                          <span className="mb-1 block font-mono text-[10px] tracking-widest text-muted-foreground uppercase">
                            Project
                          </span>
                          <span className="block break-all font-mono">
                            {job.command.project_id}
                          </span>
                        </span>
                        <span className="min-w-0">
                          <span className="mb-1 block font-mono text-[10px] tracking-widest text-muted-foreground uppercase">
                            Workflow
                          </span>
                          <span className="block break-all font-mono">
                            {job.command.workflow_id || "Project-level command"}
                          </span>
                        </span>
                      </span>
                      {job.error && (
                        <span className="block break-words text-sm text-foreground">
                          {job.error}
                        </span>
                      )}
                    </span>
                    <span className="flex flex-wrap items-center justify-between gap-3 md:flex-col md:items-end md:justify-start">
                      <Status value={job.status} />
                      <time
                        dateTime={new Date(job.created * 1000).toISOString()}
                        className="font-mono text-[11px] text-muted-foreground"
                      >
                        {new Date(job.created * 1000).toLocaleString()}
                      </time>
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState
              icon={<Activity size={24} strokeWidth={1.5} />}
              title="No activity yet"
              description="Commands appear here when you start work in a project or queue a benchmark. Open a job to inspect its result and any decisions that need review."
              action={
                <Link to="/workspace" className="text-sm font-medium underline underline-offset-4">
                  Browse projects
                </Link>
              }
              className="py-12"
            />
          )}
        </Panel>
      )}
    </div>
  );
}
