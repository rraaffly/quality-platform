import { Link, useRouterState } from "@tanstack/react-router";
import { Activity, ChartNoAxesCombined, FolderKanban, Settings2 } from "lucide-react";
import styles from "../protected-shell.module.css";

export const NAV_ITEMS = [
  { title: "Projects", to: "/workspace", icon: FolderKanban, group: "Workspace" },
  { title: "Activity", to: "/activity", icon: Activity, group: "Workspace" },
  { title: "Benchmark reports", to: "/reports", icon: ChartNoAxesCombined, group: "Evaluation" },
  { title: "Model settings", to: "/settings", icon: Settings2, group: "Evaluation" },
] as const;

export function NavMain({ query, collapsed, onNavigate }: { query: string; collapsed: boolean; onNavigate?: () => void }) {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const matches = NAV_ITEMS.filter(({ title }) => title.toLowerCase().includes(query.toLowerCase().trim()));

  return (
    <nav aria-label="Main navigation" className={styles.navigation}>
      {["Workspace", "Evaluation"].map((group) => {
        const items = matches.filter((item) => item.group === group);
        if (!items.length) return null;
        return (
          <div className={styles.navGroup} key={group}>
            <p className={styles.groupLabel}>{group}</p>
            {items.map(({ title, to, icon: Icon }) => (
              <Link
                key={to}
                to={to}
                className={styles.navLink}
                aria-label={collapsed ? title : undefined}
                aria-current={pathname.startsWith(to) ? "page" : undefined}
                title={collapsed ? title : undefined}
                onClick={onNavigate}
              >
                <Icon size={17} strokeWidth={1.8} aria-hidden="true" />
                <span className={styles.navText}>{title}</span>
              </Link>
            ))}
          </div>
        );
      })}
      {!matches.length && <p className={styles.noResults}>No matching sections</p>}
    </nav>
  );
}
