import { useRouterState, useNavigate } from "@tanstack/react-router";
import { PanelLeft } from "lucide-react";
import { Badge } from "#/components/arc/badge/badge";
import { Breadcrumb } from "#/components/arc/breadcrumb/breadcrumb";
import { Button } from "#/components/arc/button/button";
import { NAV_ITEMS } from "./navigation/nav-main";
import styles from "./protected-shell.module.css";

export function ProtectedHeader({
  onToggle,
  onOpenMobile,
  workerHealthy,
}: {
  onToggle: () => void;
  onOpenMobile: () => void;
  workerHealthy: boolean;
}) {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const navigate = useNavigate();
  const section = NAV_ITEMS.find(({ to }) => pathname.startsWith(to))?.title ?? "Projects";

  return (
    <header className={styles.header}>
      <div className={styles.headerStart}>
        <Button
          variant="ghost"
          size="sm"
          className={styles.desktopToggle}
          onClick={onToggle}
          aria-label="Toggle sidebar"
        >
          <PanelLeft size={18} aria-hidden="true" />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className={styles.mobileToggle}
          onClick={onOpenMobile}
          aria-label="Open navigation"
        >
          <PanelLeft size={18} aria-hidden="true" />
        </Button>
        <span className={styles.headerDivider} aria-hidden="true" />
        <Breadcrumb
          items={
            section === "Projects"
              ? [{ label: "Workspace" }, { label: "Projects" }]
              : [
                  {
                    label: "Workspace",
                    onClick: () => {
                      void navigate({ to: "/workspace" });
                    },
                  },
                  { label: section },
                ]
          }
        />
      </div>
      <div className={styles.headerEnd}>
        <Badge tone={workerHealthy ? "success" : "warning"} size="sm" dot>
          {workerHealthy ? "Worker online" : "Worker offline"}
        </Badge>
      </div>
    </header>
  );
}
