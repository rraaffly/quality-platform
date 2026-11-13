import type { FormEvent, ReactNode } from "react";
import { Alert } from "#/components/arc/alert/alert";
import { Badge } from "#/components/arc/badge/badge";
import { Button } from "#/components/arc/button/button";
import { useAction } from "#/lib/api";

export function Heading({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow: string;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="mb-7 flex flex-wrap items-start justify-between gap-4">
      <div>
        <p className="mb-2 text-xs font-semibold tracking-[.16em] text-primary uppercase">
          {eyebrow}
        </p>
        <h1 className="text-3xl font-bold tracking-tight text-foreground">{title}</h1>
        {description && <p className="mt-2 text-sm text-muted-foreground">{description}</p>}
      </div>
      {action}
    </div>
  );
}

export function Panel({
  title,
  children,
  action,
}: {
  title?: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <section className="min-w-0 border border-border bg-card shadow-xs">
      {title && (
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-5 py-4">
          <h2 className="font-semibold">{title}</h2>
          {action}
        </div>
      )}
      <div className="space-y-4 p-5">{children}</div>
    </section>
  );
}

export function State({ loading, error }: { loading: boolean; error: string }) {
  if (loading)
    return (
      <p role="status" className="text-sm text-muted-foreground">
        Loading…
      </p>
    );
  if (error)
    return (
      <Alert tone="danger" title="Unable to load data">
        {error}
      </Alert>
    );
  return null;
}

export function Status({ value }: { value: string }) {
  return (
    <Badge
      size="sm"
      tone={
        /FAILED|BLOCKED|REJECTED|STALE|INTERRUPTED/.test(value)
          ? "warning"
          : /APPROVED|SUCCEEDED|CURRENT|RESOLVED/.test(value)
            ? "success"
            : "neutral"
      }
    >
      {value}
    </Badge>
  );
}

export function ActionForm({
  path = "/api/commands",
  values,
  label,
  children,
  cost = false,
  review = false,
  variant = "primary",
}: {
  path?: string;
  values: Record<string, unknown>;
  label: string;
  children?: ReactNode;
  cost?: boolean;
  review?: boolean;
  variant?: "primary" | "secondary" | "danger";
}) {
  const action = useAction();
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const fields: Record<string, unknown> = Object.fromEntries(new FormData(form).entries());
    for (const name of ["replacements", "merge_with_ids"]) {
      if (name in fields) {
        try {
          fields[name] = JSON.parse(String(fields[name]));
        } catch {
          form
            .querySelector<HTMLTextAreaElement>(`[name=${name}]`)
            ?.setCustomValidity("Enter valid JSON before submitting.");
          return;
        }
      }
    }
    try {
      await action.send(path, { ...values, ...fields, ...(cost ? { confirm_cost: true } : {}) });
    } catch {
      /* The form keeps the same key and shows the error for a deliberate retry. */
    }
  }
  return (
    <form onSubmit={(event) => void submit(event)} className="space-y-3">
      {children}
      {review && (
        <label className="flex items-start gap-2 text-xs text-muted-foreground">
          <input required type="checkbox" className="mt-0.5" />I reviewed this exact version and its
          evidence.
        </label>
      )}
      {cost && (
        <label className="flex items-start gap-2 text-xs text-muted-foreground">
          <input required type="checkbox" className="mt-0.5" />I authorize this paid provider call.
          It may incur charges; no monetary cap is enforced.
        </label>
      )}
      {action.error && (
        <Alert tone="danger" title="Action not submitted">
          {action.error}
        </Alert>
      )}
      <Button type="submit" size="sm" variant={variant} loading={action.busy}>
        {label}
      </Button>
    </form>
  );
}

export const field =
  "w-full border border-border bg-background px-3 py-2 text-sm text-foreground focus:outline-primary";
export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block space-y-1 text-xs font-medium text-muted-foreground">
      <span>{label}</span>
      {children}
    </label>
  );
}
