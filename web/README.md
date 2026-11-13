# Quality Platform frontend

The React interface uses TanStack Router, Rsbuild and [Arc UI](https://uiarc.dev/) components. FastAPI serves the **built** app and its JSON API from one origin. The old Jinja2/htmx assets are no longer served.

## Build and run

```bash
cd web
bun install --frozen-lockfile
bun run build
bunx tsc --noEmit
bun run lint
```

Then start the `qa-web worker` and `qa-web serve` processes as described in [the web guide](../docs/web.md), using the same initialized state. Open the FastAPI origin, not the standalone Rsbuild dev server. The server reads `web/dist/` from this checkout; a missing build returns HTTP 503 with build instructions. `web/dist/` is ignored by Git, so build it on every machine after pulling updates. The Python package alone does not bundle the UI; run from a checkout with `web/dist/` present. Backend and frontend should not be placed on separate origins (no CORS or readable session cookies).

## Structure

- `src/routes/_app/route.tsx` contains the Signal-style workspace shell. Projects, workflows, source evidence, artifact versions, activity, reports, and settings use same-origin `/api` reads.
- `src/routes/_auth/` contains the owner sign-in screen. `GET /api/session` creates a guest session and returns the session-bound CSRF token; successful login rotates both HttpOnly cookies. API keys never reach the browser.
- `src/lib/api.ts` sends CSRF and idempotency headers on mutations. The current key is held across network retries of the same payload. Every paid call, retry, resume and reviewed QA decision needs an explicit confirmation; opening a page does not enqueue work.
- `src/components/arc/foundation.css` owns the palette, typography and component tokens. `src/assets/css/globals.css` only loads Tailwind and maps utility colors and fonts to Arc tokens; it does not redeclare the foundation.

The frontend intentionally does not run the worker or paid model calls automatically. Build assets before starting the HTTP server so it can mount the current `/static/` output.
