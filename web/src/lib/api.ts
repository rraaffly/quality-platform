import { useCallback, useEffect, useRef, useState } from "react";

export type Session = {
  actor: string | null;
  csrf: string;
  worker_healthy: boolean;
  display_name: string;
  default_password: boolean;
};
let session: Session | null = null;

export async function getSession(): Promise<Session> {
  const response = await fetch("/api/session", { credentials: "same-origin" });
  if (!response.ok) throw new Error("Unable to load your session.");
  session = (await response.json()) as Session;
  return session;
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  if (options.method && options.method !== "GET" && !session) await getSession();
  const response = await fetch(path, {
    ...options,
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...(options.body instanceof FormData
        ? {}
        : options.body
          ? { "Content-Type": "application/json" }
          : {}),
      ...(options.method && options.method !== "GET"
        ? { "X-CSRF-Token": session?.csrf ?? "" }
        : {}),
      ...options.headers,
    },
  });
  if (response.status === 401 && path !== "/login") {
    session = null;
    window.location.assign("/login");
    throw new Error("Session expired. Sign in again.");
  }
  if (!response.ok) {
    const error = (await response.json().catch(() => ({}))) as { detail?: string };
    throw new Error(error.detail ?? `Request failed (${response.status}).`);
  }
  return (await response.json()) as T;
}

export function setSession(next: Session | null) {
  session = next;
}
export function resourcePath(...parts: string[]) {
  return parts.map(encodeURIComponent).join("/");
}

export type Job = {
  id: string;
  status: string;
  error: string | null;
  result: Record<string, unknown> | null;
  command: Record<string, unknown> & {
    kind: string;
    project_id: string;
    workflow_id?: string;
    request_id?: string;
  };
  created: number;
  started: number | null;
  finished: number | null;
  worker_healthy?: boolean;
  diff?: string;
  current_hash?: string;
};
export type Project = {
  id: string;
  name: string;
  workflows: { id: string; goal: string; status: string }[];
};
export type Workflow = {
  workflow: { id: string; goal: string; status: string };
  requirements: Array<{
    id: string;
    version: number;
    readiness: string;
    statement: string;
    dependency_ids: string[];
    source_references: Array<{ document_id: string; start_line: number; end_line: number }>;
  }>;
  clarifications: Array<{
    id: string;
    issue_type: string;
    status: string;
    question: string;
    resolution?: string;
    answers: Array<{ text: string; author: string; source: string; recorded_by: string }>;
  }>;
  scopes: Array<{
    id: string;
    journey: string;
    status: string;
    requirement_ids: string[];
    excluded_requirement_ids: string[];
    dependency_scope_ids: string[];
  }>;
  baselines: Array<{ id: string; scope_id: string; actor: string; requirement_versions: unknown }>;
  artifacts: Array<{
    id: string;
    version: number;
    kind: string;
    status: string;
    staleness: string;
    scope_id: string;
    content_hash: string;
    requirement_baseline_id: string;
  }>;
  sources: Array<{ id: string; name: string; hash: string; lines: number }>;
  history: Array<{ seq: number; type: string; target: string; data: Record<string, unknown> }>;
  snapshot: {
    agent_models: Record<string, { provider: string; model_id: string }>;
    max_concurrency: number;
    test_case_style: string;
  } | null;
  state_hash: string;
};

export function useResource<T>(path: string, poll = false) {
  const [result, setResult] = useState<{ path: string; data?: T; error: string }>({
    path,
    error: "",
  });
  const reload = useCallback(async () => {
    try {
      const data = await api<T>(path);
      setResult({ path, data, error: "" });
    } catch (cause) {
      setResult({ path, error: (cause as Error).message });
    }
  }, [path]);
  useEffect(() => {
    void reload();
    if (poll) {
      const timer = window.setInterval(() => void reload(), 3000);
      return () => window.clearInterval(timer);
    }
  }, [reload, poll]);
  return {
    data: result.path === path ? result.data : undefined,
    error: result.path === path ? result.error : "",
    loading: result.path !== path || (!result.data && !result.error),
    reload,
  };
}

export function useAction<T = object>() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const key = useRef<{ payload: string; value: string } | null>(null);
  const send = async (path: string, body: Record<string, unknown> | FormData) => {
    const payload =
      body instanceof FormData
        ? JSON.stringify(
            [...body.entries()].map(([name, value]) => [
              name,
              value instanceof File ? [value.name, value.size, value.lastModified] : value,
            ]),
          )
        : JSON.stringify(body);
    const identity = path + payload;
    if (key.current?.payload !== identity)
      key.current = { payload: identity, value: crypto.randomUUID() };
    setBusy(true);
    setError("");
    try {
      const result = await api<T & { job_id?: string; settings_id?: string }>(path, {
        method: "POST",
        body: body instanceof FormData ? body : JSON.stringify(body),
        headers: { "Idempotency-Key": key.current.value },
      });
      key.current = null;
      if (result.job_id) window.location.assign(`/activity/${encodeURIComponent(result.job_id)}`);
      return result;
    } catch (cause) {
      setError((cause as Error).message);
      throw cause;
    } finally {
      setBusy(false);
    }
  };
  return { send, busy, error };
}
