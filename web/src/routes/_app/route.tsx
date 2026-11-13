import { useEffect, useState } from "react";
import { createFileRoute, Outlet } from "@tanstack/react-router";
import { ProtectedHeader, ProtectedSidebar } from "#/components/layout";
import styles from "#/components/layout/protected/protected-shell.module.css";
import { getSession, type Session } from "#/lib/api";
import { State } from "#/features/workspace/ui";

export const Route = createFileRoute("/_app")({
  component: AppLayout,
});

function AppLayout() {
  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [session, setSession] = useState<Session>();
  const [error, setError] = useState("");
  useEffect(() => {
    void getSession()
      .then((value) => {
        if (!value.actor) window.location.replace("/login");
        else setSession(value);
      })
      .catch((cause: Error) => setError(cause.message));
  }, []);

  if (!session)
    return (
      <div className="p-8">
        <State loading={!error} error={error} />
      </div>
    );

  return (
    <div className={styles.shell}>
      <a href="#main" className={styles.skip}>
        Skip to content
      </a>
      <ProtectedSidebar
        actor={session.actor ?? "Owner"}
        collapsed={collapsed}
        onToggle={() => setCollapsed((value) => !value)}
        mobileOpen={mobileOpen}
        onMobileOpenChange={setMobileOpen}
      />
      <main className={styles.main}>
        <ProtectedHeader
          workerHealthy={session.worker_healthy}
          onToggle={() => setCollapsed((value) => !value)}
          onOpenMobile={() => setMobileOpen(true)}
        />
        <div id="main" className={styles.content} tabIndex={-1}>
          <Outlet />
        </div>
      </main>
    </div>
  );
}
