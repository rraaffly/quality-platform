import { cloneElement, useId, type FormEvent, type ReactElement, type ReactNode } from "react";
import { Alert } from "#/components/arc/alert/alert";
import { Badge } from "#/components/arc/badge/badge";
import { Button } from "#/components/arc/button/button";
import { useAction } from "#/lib/api";
import styles from "./ui.module.css";

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
    <div className={styles.heading}>
      <div className="min-w-0">
        <p className={styles.eyebrow}>{eyebrow}</p>
        <h1 className={styles.title}>{title}</h1>
        {description && <p className={styles.description}>{description}</p>}
      </div>
      {action && <div className={styles.headingAction}>{action}</div>}
    </div>
  );
}

export function Panel({
  title,
  description,
  children,
  action,
}: {
  title?: string;
  description?: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <section className={styles.panel}>
      {title && (
        <div className={styles.panelHeader}>
          <div className="min-w-0">
            <h2>{title}</h2>
            {description && <p>{description}</p>}
          </div>
          {action}
        </div>
      )}
      <div className={styles.panelBody}>{children}</div>
    </section>
  );
}

export function Summary({
  items,
}: {
  items: { label: string; value: ReactNode; detail?: string }[];
}) {
  return (
    <dl className={styles.summary}>
      {items.map(({ label, value, detail }) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
          {detail && <p>{detail}</p>}
        </div>
      ))}
    </dl>
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
      className="font-mono text-[10px]"
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

export const field = styles.control;
export function Field({
  label,
  children,
}: {
  label: string;
  children: ReactElement<{ id?: string }>;
}) {
  const generatedId = useId();
  const id = children.props.id ?? generatedId;
  return (
    <div className={styles.field}>
      <label htmlFor={id}>{label}</label>
      {cloneElement(children, { id })}
    </div>
  );
}
