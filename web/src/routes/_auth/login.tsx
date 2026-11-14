import { AuthPage } from "#/components/layout";
import { LoginForm } from "#/features/auth";
import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/_auth/login")({
  component: LoginPage,
});

function LoginPage() {
  return (
    <AuthPage id="login">
      <div className="w-full space-y-6">
        <div className="space-y-1.5 text-left">
          <h2 className="text-2xl font-bold tracking-tight text-foreground">Welcome back</h2>
          <p className="text-sm text-muted-foreground">Enter your dashboard password to sign in.</p>
        </div>

        <LoginForm />
      </div>
    </AuthPage>
  );
}
