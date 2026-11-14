import { Button } from "#/components/arc/button/button";
import { Input } from "#/components/arc/input/input";
import { Alert } from "#/components/arc/alert/alert";
import { api, getSession, setSession, type Session } from "#/lib/api";
import { useEffect, useState, type FormEvent } from "react";

export function LoginForm() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [defaultPassword, setDefaultPassword] = useState(false);
  const [passwordChanged] = useState(
    () => sessionStorage.getItem("account-password-changed") === "true",
  );
  useEffect(() => {
    sessionStorage.removeItem("account-password-changed");
    void getSession()
      .then((value) => setDefaultPassword(value.default_password))
      .catch((cause: Error) => setError(cause.message));
  }, []);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const form = new FormData(event.currentTarget);
    try {
      const current = await getSession();
      setDefaultPassword(current.default_password);
      const next = await api<Session>("/login", {
        method: "POST",
        body: JSON.stringify({ password: form.get("password") }),
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
      {passwordChanged && (
        <Alert tone="success" title="Password updated">
          All sessions have been signed out. Sign in with your new password.
        </Alert>
      )}
      {defaultPassword && (
        <Alert tone="warning" title="Initial password">
          Sign in with 1234567890, then replace this password in Settings.
        </Alert>
      )}
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
