import { AuthPage } from "#/components/layout";
import { LoginForm } from "#/features/auth";
import { createFileRoute } from "@tanstack/react-router";
import { LockKeyhole } from "lucide-react";

export const Route = createFileRoute("/_auth/login")({
  component: LoginPage,
});

function LoginPage() {
  return (
    <AuthPage id="login">
      <div className="border border-border bg-card">
        <div className="flex items-center justify-between border-b border-border px-7 py-4">
          <span className="font-mono text-[10px] tracking-widest text-muted-foreground uppercase">
            Owner access
          </span>
          <LockKeyhole size={14} className="text-muted-foreground" aria-hidden="true" />
        </div>
        <div className="space-y-7 p-7 sm:p-8">
          <div>
            <h1 className="text-[28px] font-medium tracking-tight">Sign in</h1>
            <p className="mt-2 text-[13px] leading-relaxed text-muted-foreground">
              Enter your password to open the dashboard.
            </p>
          </div>
          <LoginForm />
        </div>
      </div>
      <p className="mt-5 text-center font-mono text-[10px] text-muted-foreground">
        Password-protected session
      </p>
    </AuthPage>
  );
}
