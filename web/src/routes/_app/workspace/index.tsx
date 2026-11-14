import { Link, createFileRoute } from "@tanstack/react-router";
import { Alert } from "#/components/arc/alert/alert";
import { EmptyState } from "#/components/arc/empty-state/empty-state";
import { Button } from "#/components/arc/button/button";
import { ArrowUpRight, FolderOpen, Plus } from "lucide-react";
import {
  ActionForm,
  Field,
  Heading,
  Panel,
  State,
  Status,
  Summary,
  field,
} from "#/features/workspace/ui";
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
        eyebrow="Workspace / Overview"
        title="Projects"
        description="Source evidence, requirements and review decisions for each project."
        action={
          <Button
            variant="secondary"
            onClick={() => document.getElementById("new-project-id")?.focus()}
          >
            <Plus size={14} aria-hidden="true" />
            New project
          </Button>
        }
      />
      <State loading={loading} error={error} />
      {data && (
        <>
          <Summary
            items={[
              {
                label: "Projects",
                value: String(data.projects.length).padStart(2, "0"),
                detail: "Local project workspaces",
              },
              {
                label: "Workflows",
                value: String(
                  data.projects.reduce((sum, project) => sum + project.workflows.length, 0),
                ).padStart(2, "0"),
                detail: "Across all projects",
              },
              {
                label: "Agent models",
                value: data.models_configured ? "Configured" : "Not configured",
                detail: "Managed in Settings",
              },
            ]}
          />
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
          <div className="grid items-start gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
            <div className="space-y-4">
              {!data.projects.length && (
                <Panel
                  title="Your projects"
                  action={
                    <span className="font-mono text-[10px] text-muted-foreground">0 PROJECTS</span>
                  }
                >
                  <EmptyState
                    title="Start with your first project"
                    description="Organize QA work and source snapshots. Creating a project does not call a model."
                    icon={<FolderOpen size={23} strokeWidth={1.5} />}
                    action={
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() => document.getElementById("new-project-id")?.focus()}
                      >
                        Create a project <ArrowUpRight size={14} aria-hidden="true" />
                      </Button>
                    }
                  />
                </Panel>
              )}
              {data.projects.map((project) => (
                <Panel
                  key={project.id}
                  title={project.name}
                  description={`${project.workflows.length} ${project.workflows.length === 1 ? "workflow" : "workflows"}`}
                  action={
                    <span className="max-w-full break-all font-mono text-[10px] text-muted-foreground">
                      {project.id}
                    </span>
                  }
                >
                  {project.workflows.length ? (
                    project.workflows.map((workflow) => (
                      <Link
                        key={workflow.id}
                        to="/workspace/$project/$workflow"
                        params={{ project: project.id, workflow: workflow.id }}
                        className="group flex flex-wrap items-center justify-between gap-4 border border-border p-4 transition-colors hover:bg-muted/50"
                      >
                        <span className="min-w-0 flex-1 basis-40">
                          <strong className="block break-words text-[13px] font-medium leading-relaxed">
                            {workflow.goal}
                          </strong>
                          <small className="mt-1 block break-all font-mono text-[10px] text-muted-foreground">
                            {workflow.id}
                          </small>
                        </span>
                        <span className="flex items-center gap-3">
                          <Status value={workflow.status} />
                          <ArrowUpRight
                            size={15}
                            className="text-muted-foreground group-hover:text-foreground"
                            aria-hidden="true"
                          />
                        </span>
                      </Link>
                    ))
                  ) : (
                    <div className="border border-dashed border-border px-4 py-6 text-center">
                      <p className="text-[13px] text-muted-foreground">No workflows yet.</p>
                      <p className="mt-1 text-xs text-muted-foreground">
                        Create a workflow to begin reviewing source evidence.
                      </p>
                    </div>
                  )}
                  <details className="border-t border-border pt-3">
                    <summary className="cursor-pointer py-1 text-xs font-medium">
                      Create a workflow
                    </summary>
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
            <Panel
              title="New project"
              description="A dedicated space for requirements, source evidence, and QA workflows."
            >
              <ActionForm values={{ kind: "create_project" }} label="Create project">
                <Field label="Project ID">
                  <input
                    className={field}
                    id="new-project-id"
                    name="project_id"
                    required
                    pattern="[A-Za-z0-9][A-Za-z0-9_.-]{0,99}"
                    placeholder="customer-portal"
                  />
                </Field>
                <Field label="Project name">
                  <input
                    className={field}
                    name="name"
                    required
                    maxLength={200}
                    placeholder="Customer portal"
                  />
                </Field>
                <p className="pb-2 text-[11px] leading-relaxed text-muted-foreground">
                  Use a short, unique ID. Letters, numbers, dots, hyphens, and underscores are
                  supported.
                </p>
              </ActionForm>
              <p className="border-t border-border pt-4 text-[11px] leading-relaxed text-muted-foreground">
                Stored in your configured workspace directory. Creating a project does not call a
                model or incur provider charges.
              </p>
            </Panel>
          </div>
        </>
      )}
    </div>
  );
}
