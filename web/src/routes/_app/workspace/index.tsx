import { Link, createFileRoute } from "@tanstack/react-router";
import { Alert } from "#/components/arc/alert/alert";
import { EmptyState } from "#/components/arc/empty-state/empty-state";
import { ActionForm, Field, Heading, Panel, State, Status, field } from "#/features/workspace/ui";
import { type Project, useResource } from "#/lib/api";

export const Route = createFileRoute("/_app/workspace/")({ component: ProjectsPage });

function ProjectsPage() {
  const { data, error, loading } = useResource<{
    projects: Project[];
    settings_id: string;
    models_configured: boolean;
    worker_healthy: boolean;
  }>("/api/projects");
  return (
    <div className="space-y-6">
      <Heading
        eyebrow="Quality workspace"
        title="Projects"
        description="Source evidence, requirements and review decisions for each project."
      />
      <State loading={loading} error={error} />
      {data && (
        <>
          {!data.models_configured && (
            <Alert tone="warning" title="Models not configured">
              You can create projects now. Before creating workflows or running benchmarks,
              configure all agent models in{" "}
              <Link to="/settings" className="underline">
                Settings
              </Link>
              .
            </Alert>
          )}
          {!data.worker_healthy && (
            <Alert tone="warning" title="Worker offline">
              Commands can be queued, but processing will not start until the worker is running.
            </Alert>
          )}
          <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_19rem]">
            <div className="space-y-4">
              {!data.projects.length && (
                <Panel title="Your projects">
                  <EmptyState
                    title="Start with your first project"
                    description="Organize QA work and source snapshots. Creating a project does not call a model."
                  />
                </Panel>
              )}
              {data.projects.map((project) => (
                <Panel
                  key={project.id}
                  title={project.name}
                  action={<span className="text-xs text-muted-foreground">{project.id}</span>}
                >
                  {project.workflows.length ? (
                    project.workflows.map((workflow) => (
                      <Link
                        key={workflow.id}
                        to="/workspace/$project/$workflow"
                        params={{ project: project.id, workflow: workflow.id }}
                        className="flex items-center justify-between gap-4 border border-border p-3 hover:bg-muted"
                      >
                        <span>
                          <strong className="block text-sm">{workflow.goal}</strong>
                          <small className="text-muted-foreground">{workflow.id}</small>
                        </span>
                        <Status value={workflow.status} />
                      </Link>
                    ))
                  ) : (
                    <p className="text-sm text-muted-foreground">No workflows yet.</p>
                  )}
                  <details className="border-t border-border pt-3">
                    <summary className="cursor-pointer text-sm font-medium">New workflow</summary>
                    <div className="pt-3">
                      <ActionForm
                        values={{
                          kind: "create_workflow",
                          project_id: project.id,
                          settings_id: data.settings_id,
                        }}
                        label="Create workflow"
                      >
                        <Field label="Workflow ID">
                          <input
                            className={field}
                            name="workflow_id"
                            required
                            pattern="[A-Za-z0-9][A-Za-z0-9_.-]{0,99}"
                            placeholder="login-review"
                          />
                        </Field>
                        <Field label="Goal">
                          <textarea className={field} name="goal" required rows={3} />
                        </Field>
                        <p className="text-xs text-muted-foreground">
                          Pins the current model settings. No provider call is made.
                        </p>
                      </ActionForm>
                    </div>
                  </details>
                </Panel>
              ))}
            </div>
            <Panel title="New project">
              <ActionForm values={{ kind: "create_project" }} label="Create project">
                <Field label="Project ID">
                  <input
                    className={field}
                    name="project_id"
                    required
                    pattern="[A-Za-z0-9][A-Za-z0-9_.-]{0,99}"
                    placeholder="customer-portal"
                  />
                </Field>
                <Field label="Project name">
                  <input className={field} name="name" required maxLength={200} />
                </Field>
              </ActionForm>
              <p className="text-xs text-muted-foreground">
                Projects and their snapshots are stored in server-managed directories.
              </p>
            </Panel>
          </div>
        </>
      )}
    </div>
  );
}
