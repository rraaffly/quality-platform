import { createFileRoute } from "@tanstack/react-router";
import { Heading, Panel, State, Status } from "#/features/workspace/ui";
import { EmptyState } from "#/components/arc/empty-state/empty-state";
import { useResource } from "#/lib/api";

type Report = {
  id: string;
  project_id: string;
  report: { run_id: string; decision: string; dataset_version: string; [key: string]: unknown };
};
export const Route = createFileRoute("/_app/reports")({ component: ReportsPage });

function ReportsPage() {
  const { data, loading, error } = useResource<{ reports: Report[] }>("/api/reports");
  return (
    <div className="space-y-5">
      <Heading
        eyebrow="Evaluation"
        title="Benchmark reports"
        description="Registered summaries only. Viewing them does not run paid benchmarks or approve a release."
        action={<Status value="Read-only" />}
      />
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
              <pre className="max-h-[36rem] overflow-auto whitespace-pre-wrap break-words text-xs">
                {JSON.stringify(entry.report, null, 2)}
              </pre>
            </Panel>
          ))
        ) : (
          <Panel>
            <EmptyState
              title="No registered reports"
              description="Register a local benchmark report through the CLI. An empty dashboard is not evidence of release readiness."
            />
          </Panel>
        ))}
    </div>
  );
}
