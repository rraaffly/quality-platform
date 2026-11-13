import { useState } from "react";
import { Link } from "@tanstack/react-router";
import {
  PanelLeftClose,
  PanelLeftOpen,
  Search,
  ShieldCheck,
  SlidersHorizontal,
} from "lucide-react";
import { Avatar } from "#/components/arc/avatar/avatar";
import { Drawer, DrawerContent } from "#/components/arc/drawer/drawer";
import { api, setSession } from "#/lib/api";
import { NavMain } from "./navigation/nav-main";
import styles from "./protected-shell.module.css";

const STAGES = ["Requirement analysis", "Scope review", "Test planning", "Test design"];

function SidebarContents({
  collapsed,
  actor,
  onNavigate,
}: {
  collapsed: boolean;
  actor: string;
  onNavigate?: () => void;
}) {
  const [query, setQuery] = useState("");

  return (
    <div className={styles.sidebarInner} data-collapsed={collapsed}>
      <div className={styles.brandRow}>
        <Link
          to="/workspace"
          onClick={onNavigate}
          className={styles.brand}
          aria-label="Quality Platform home"
        >
          <span className={styles.brandMark}>
            <ShieldCheck size={19} strokeWidth={1.9} aria-hidden="true" />
          </span>
          <span className={styles.brandName}>Quality Platform</span>
        </Link>
      </div>

      <div className={styles.sidebarScroll}>
        <div className={styles.searchWrap}>
          <Search size={16} aria-hidden="true" />
          <input
            type="search"
            aria-label="Search sections"
            placeholder="Search sections..."
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") setQuery("");
            }}
          />
        </div>
        <NavMain query={query} collapsed={collapsed} onNavigate={onNavigate} />
        {!query && (
          <div className={styles.stages}>
            <p className={styles.groupLabel}>Workflow stages</p>
            <ol>
              {STAGES.map((stage, index) => (
                <li key={stage}>
                  <span>{String(index + 1).padStart(2, "0")}</span>
                  {stage}
                </li>
              ))}
            </ol>
          </div>
        )}
      </div>

      <div className={styles.sidebarBottom}>
        <div className={styles.previewCard}>
          <div className={styles.previewCardIcon}>
            <SlidersHorizontal size={15} aria-hidden="true" />
          </div>
          <strong>Review before running</strong>
          <p>Model calls require explicit cost confirmation.</p>
        </div>
        <div className={styles.account}>
          <Avatar name={actor} size="sm" />
          <span className={styles.accountText}>
            <strong>{actor}</strong>
            <small>Owner account</small>
          </span>
          <button
            type="button"
            className="text-xs underline"
            onClick={() => {
              void api("/logout", { method: "POST", body: "{}" }).then(() => {
                setSession(null);
                window.location.assign("/login");
              });
            }}
          >
            Sign out
          </button>
        </div>
      </div>
    </div>
  );
}

export function ProtectedSidebar({
  collapsed,
  actor,
  onToggle,
  mobileOpen,
  onMobileOpenChange,
}: {
  actor: string;
  collapsed: boolean;
  onToggle: () => void;
  mobileOpen: boolean;
  onMobileOpenChange: (open: boolean) => void;
}) {
  return (
    <>
      <aside className={styles.sidebar} data-collapsed={collapsed} aria-label="Workspace sidebar">
        <SidebarContents collapsed={collapsed} actor={actor} />
        <button
          type="button"
          className={styles.rail}
          onClick={onToggle}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        >
          {collapsed ? (
            <PanelLeftOpen size={16} aria-hidden="true" />
          ) : (
            <PanelLeftClose size={16} aria-hidden="true" />
          )}
        </button>
      </aside>
      <Drawer open={mobileOpen} onOpenChange={onMobileOpenChange}>
        <DrawerContent title="Quality Platform" side="left" className={styles.mobileDrawer}>
          <SidebarContents
            collapsed={false}
            actor={actor}
            onNavigate={() => onMobileOpenChange(false)}
          />
        </DrawerContent>
      </Drawer>
    </>
  );
}
