import { Link, createFileRoute } from "@tanstack/react-router";
import { Heading, Panel, State } from "#/features/workspace/ui";
import { resourcePath, useResource } from "#/lib/api";

export const Route = createFileRoute("/_app/workspace/$project/$workflow/sources/$source")({
  component: SourcePage,
});

function SourcePage() {
  const { project, workflow, source } = Route.useParams();
  const { data, loading, error } = useResource<{ id: string; content: string }>(
    `/api/projects/${resourcePath(project)}/workflows/${resourcePath(workflow)}/sources/${resourcePath(source)}`,
  );
  return (
    <div className="space-y-5">
      <Link
        to="/workspace/$project/$workflow"
        params={{ project, workflow }}
        className="text-xs text-primary hover:underline"
      >
        ← {workflow} / Evidence
      </Link>
      <Heading eyebrow="Immutable snapshot" title="Source evidence" description={source} />
      <State loading={loading} error={error} />
      {data && (
        <Panel>
          {data.content.split(/\r?\n/).map((line, index) => (
            <div
              key={index}
              id={`line-${index + 1}`}
              className="flex gap-4 border-b border-border/50 font-mono text-xs scroll-mt-4"
            >
              <a
                href={`#line-${index + 1}`}
                className="w-8 shrink-0 text-right text-muted-foreground"
              >
                {index + 1}
              </a>
              <code className="min-w-0 whitespace-pre-wrap break-all">{line || " "}</code>
            </div>
          ))}
        </Panel>
      )}
    </div>
  );
}
