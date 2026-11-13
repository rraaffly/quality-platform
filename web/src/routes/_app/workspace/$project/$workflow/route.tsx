import { Outlet, createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/_app/workspace/$project/$workflow")({ component: Outlet });
