import { Outlet, createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/_app/activity")({ component: Outlet });
