import { Button } from "#/components/arc/button/button";
import { Input } from "#/components/arc/input/input";
import { Alert } from "#/components/arc/alert/alert";
import { api, getSession, setSession, type Session } from "#/lib/api";
import { useState, type FormEvent } from "react";

export function LoginForm() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const form = new FormData(event.currentTarget);
    try {
      await getSession();
      const next = await api<Session>("/login", {
        method: "POST",
        body: JSON.stringify({ owner: form.get("owner"), password: form.get("password") }),
      });
      setSession(next);
      window.location.assign("/workspace");
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="space-y-5" onSubmit={(event) => void submit(event)}>
      <Input
        label="Owner"
        id="owner"
        name="owner"
        autoComplete="username"
        placeholder="Your owner name"
        required
      />
      <Input
        label="Password"
        id="password"
        name="password"
        type="password"
        autoComplete="current-password"
        placeholder="Your password"
        required
      />
      {error && (
        <Alert tone="danger" title="Sign-in failed">
          {error}
        </Alert>
      )}
      <Button type="submit" loading={busy} className="w-full">
        Sign in to workspace
      </Button>
    </form>
  );
}
