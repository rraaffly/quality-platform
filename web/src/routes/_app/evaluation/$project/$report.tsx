import { createFileRoute } from "@tanstack/react-router";
import type { FormEvent } from "react";
import { Alert } from "#/components/arc/alert/alert";
import { Button } from "#/components/arc/button/button";
import { Field, Heading, Panel, State, field } from "#/features/workspace/ui";
import { useAction, useResource } from "#/lib/api";

type ObservedItem = { id: string; kind: string; content: string };
type Packet = {
  candidate_run_id: string;
  repeat_run_ids: string[];
  outputs: Array<Record<string, { observed_items: ObservedItem[] }>>;
};

export const Route = createFileRoute("/_app/evaluation/$project/$report")({
  component: ReviewPage,
});

function ReviewPage() {
  const { project, report } = Route.useParams();
  const packet = useResource<Packet>(
    `/api/projects/${encodeURIComponent(project)}/evaluation/${encodeURIComponent(report)}/review-packet`,
  );
  const action = useAction();
  const caseIds = Object.keys(packet.data?.outputs[0] ?? {}).sort();
  const base = { kind: "evaluation_action", project_id: project, report_id: report };

  async function submitReview(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const reviewer = String(form.get("reviewer") ?? "").trim();
    const reviewedAt = new Date().toISOString();
    const reviews = caseIds.map((caseId, index) => ({
      case_id: caseId,
      rating: Number(form.get(`rating-${index}`)),
      critical: form.get(`critical-${index}`) === "on",
      reviewer,
      reviewed_at: reviewedAt,
      blind: true,
      model_snapshot_id: null,
      uncertainty: Number(form.get(`uncertainty-${index}`)),
    }));
    try {
      await action.send("/api/evaluation/jobs", {
        ...base,
        action: "capture_review",
        reviews,
        confirm_independent: form.get("confirm_independent") === "on",
        confirm_all_repeats_reviewed: form.get("confirm_all_repeats_reviewed") === "on",
      });
    } catch {
      // The action displays the error.
    }
  }

  async function submitProductivity(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      const form = new FormData(event.currentTarget);
      const productivity = JSON.parse(String(form.get("productivity") ?? "")) as Record<
        string,
        unknown
      >;
      await action.send("/api/evaluation/jobs", {
        ...base,
        action: "record_productivity",
        productivity,
      });
    } catch {
      // The action displays server errors; malformed JSON remains in the form.
    }
  }

  async function submitCalibration(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      await action.send("/api/evaluation/jobs", {
        ...base,
        action: "calibrate",
        human_id: form.get("human_id"),
      });
    } catch {
      // The action displays the error.
    }
  }

  async function submitRelease(event: FormEvent<HTMLFormElement>, stage: "prepare_release" | "review_release" | "promote_baseline") {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      const thresholds = JSON.parse(String(form.get("thresholds") ?? "{}")) as Record<string, unknown>;
      const payload: Record<string, unknown> = {
        ...base,
        action: stage,
        human_id: String(form.get("human_id") ?? ""),
        calibration_id: String(form.get("calibration_id") ?? ""),
        thresholds,
      };
      if (stage === "review_release") {
        payload.bootstrap = form.get("bootstrap") === "on";
        payload.approval = {
          candidate_run_id: packet.data?.candidate_run_id,
          status: String(form.get("status") ?? "REJECTED"),
          reviewer: String(form.get("reviewer") ?? ""),
          reviewed_at: new Date().toISOString(),
          reviewed_evidence: String(form.get("reviewed_evidence") ?? "")
            .split("\n")
            .map((line) => line.trim())
            .filter(Boolean),
        };
      }
      if (stage === "promote_baseline") {
        payload.release_id = String(form.get("release_id") ?? "");
        payload.confirm_promotion = form.get("confirm_promotion") === "on";
      }
      await action.send("/api/evaluation/jobs", payload);
    } catch {
      // The action displays server errors; malformed JSON remains editable.
    }
  }

  return (
    <div className="space-y-5">
      <Heading
        eyebrow="Independent review"
        title={`Review ${packet.data?.candidate_run_id ?? report.slice(0, 8)}`}
        description="Rate candidate outputs before opening evaluator findings. Review every repeat and record the worst correctness for each case."
      />
      <State loading={packet.loading} error={packet.error} />
      {packet.data && (
        <>
          <Panel title="Candidate outputs">
            <p className="mb-3 text-sm text-muted-foreground">
              {packet.data.outputs.length} repeat(s). The private packet contains candidate content
              and no AI ratings.
            </p>
            {caseIds.map((caseId) => (
              <div key={caseId} className="border-t border-border py-3">
                <strong>{caseId}</strong>
                {packet.data!.outputs.map((repeat, index) => (
                  <div key={index} className="mt-2 border-l-2 border-primary pl-3 text-sm">
                    <small className="text-muted-foreground">Repeat {index + 1}</small>
                    {repeat[caseId]?.observed_items.map((item) => (
                      <p key={item.id}>
                        {item.id} · {item.kind} · {item.content}
                      </p>
                    ))}
                  </div>
                ))}
              </div>
            ))}
          </Panel>
          <Panel title="Capture blind human ratings">
            <form onSubmit={(event) => void submitReview(event)} className="space-y-4">
              <Field label="Reviewer">
                <input className={field} name="reviewer" required />
              </Field>
              {caseIds.map((caseId, index) => (
                <div key={caseId} className="grid gap-3 border-t border-border pt-3 md:grid-cols-4">
                  <strong>{caseId}</strong>
                  <Field label="Rating (1–5)">
                    <input
                      className={field}
                      name={`rating-${index}`}
                      type="number"
                      min="1"
                      max="5"
                      required
                    />
                  </Field>
                  <Field label="Uncertainty (0–1)">
                    <input
                      className={field}
                      name={`uncertainty-${index}`}
                      type="number"
                      min="0"
                      max="1"
                      step="0.01"
                      defaultValue="0"
                      required
                    />
                  </Field>
                  <label className="flex items-center gap-2 text-sm">
                    <input type="checkbox" name={`critical-${index}`} />
                    Critical violation
                  </label>
                </div>
              ))}
              <label className="flex gap-2 text-sm">
                <input type="checkbox" name="confirm_independent" required />I rated these outputs
                before seeing the evaluator ratings.
              </label>
              {packet.data.outputs.length > 1 && (
                <label className="flex gap-2 text-sm">
                  <input type="checkbox" name="confirm_all_repeats_reviewed" required />I reviewed
                  every repeat.
                </label>
              )}
              <Button type="submit" loading={action.busy}>
                Save independent ratings
              </Button>
            </form>
          </Panel>
          <Panel title="Additional evidence">
            <div className="space-y-4">
              <Button
                variant="secondary"
                loading={action.busy}
                onClick={() =>
                  void action.send("/api/evaluation/jobs", { ...base, action: "capture_workflow" })
                }
              >
                Run offline workflow suite
              </Button>
              <form onSubmit={(event) => void submitProductivity(event)} className="space-y-2">
                <Field label="Equivalent-task productivity evidence (JSON)">
                  <textarea className={field} name="productivity" rows={6} required />
                </Field>
                <Button type="submit" variant="secondary" loading={action.busy}>
                  Attach productivity evidence
                </Button>
              </form>
              <form onSubmit={(event) => void submitCalibration(event)} className="space-y-2">
                <Field label="Human review ID from the completed job">
                  <input className={field} name="human_id" required />
                </Field>
                <Button type="submit" variant="secondary" loading={action.busy}>
                  Reveal evaluator calibration
                </Button>
              </form>
            </div>
          </Panel>
          <Panel title="Release decision">
            <p className="mb-4 text-sm text-muted-foreground">
              Use the latest report ID after attaching workflow and productivity evidence. Copy IDs and
              evidence hashes from each completed job. Thresholds must be calibrated before approval.
            </p>
            <form
              onSubmit={(event) => {
                const stage = String((event.nativeEvent as SubmitEvent).submitter?.getAttribute("value")) as
                  | "prepare_release"
                  | "review_release"
                  | "promote_baseline";
                void submitRelease(event, stage);
              }}
              className="space-y-3"
            >
              <Field label="Human review ID"><input className={field} name="human_id" required /></Field>
              <Field label="Calibration ID"><input className={field} name="calibration_id" required /></Field>
              <Field label="Calibrated quality thresholds (JSON)">
                <textarea className={field} name="thresholds" rows={9} defaultValue={JSON.stringify({
                  version: "v1", status: "ILLUSTRATIVE", minimum_precision: 0.95,
                  minimum_recall: 0.95, minimum_evidence_accuracy: 0.98,
                  maximum_precision_regression: 0.02, maximum_recall_regression: 0.02,
                  maximum_evidence_regression: 0.02,
                }, null, 2)} required />
              </Field>
              <Button type="submit" value="prepare_release" variant="secondary" loading={action.busy}>
                Prepare exact evidence hashes
              </Button>
              <Field label="Evidence hashes from prepare job, one per line">
                <textarea className={field} name="reviewed_evidence" rows={7} />
              </Field>
              <Field label="Release reviewer"><input className={field} name="reviewer" /></Field>
              <Field label="Decision">
                <select className={field} name="status" defaultValue="REJECTED">
                  <option value="REJECTED">Rejected</option>
                  <option value="APPROVED">Approved</option>
                </select>
              </Field>
              <label className="flex gap-2 text-sm"><input type="checkbox" name="bootstrap" />First approved baseline</label>
              <Button type="submit" value="review_release" variant="secondary" loading={action.busy}>
                Record release review
              </Button>
              <Field label="Release review ID from completed job"><input className={field} name="release_id" /></Field>
              <label className="flex gap-2 text-sm"><input type="checkbox" name="confirm_promotion" />Promote this approved release as the baseline</label>
              <Button type="submit" value="promote_baseline" loading={action.busy}>
                Promote approved baseline
              </Button>
            </form>
          </Panel>
          {action.error && (
            <Alert tone="danger" title="Action failed">
              {action.error}
            </Alert>
          )}
        </>
      )}
    </div>
  );
}
