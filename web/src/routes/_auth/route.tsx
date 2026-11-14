import { createFileRoute, Outlet } from "@tanstack/react-router";

export const Route = createFileRoute("/_auth")({
  component: AuthLayout,
});

function AuthLayout() {
  return (
    <main className="flex min-h-svh items-center justify-center bg-muted/30 px-5 py-12 font-sans text-foreground">
      <Outlet />
    </main>
  );
}
