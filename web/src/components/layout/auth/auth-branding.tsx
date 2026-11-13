import {
  ArrowUpRight,
  CheckCheck,
  FileText,
  FolderKanban,
  ListChecks,
  ShieldCheck,
} from "lucide-react";
import "./auth-branding.css";

const MATRIX_CELLS = Array.from({ length: 35 }, (_, index) => ({
  index,
  delay: (index % 7) * 0.22 + Math.floor(index / 7) * 0.1,
  active: [2, 3, 4, 8, 12, 14, 18, 20, 21, 25, 27, 30, 31, 32].includes(index),
}));

const CAPABILITIES = [
  { label: "Project workspaces", icon: FolderKanban },
  { label: "Source evidence", icon: FileText },
  { label: "Test planning", icon: ListChecks },
] as const;

function QualityMatrix() {
  return (
    <div
      className="relative w-[56%] shrink-0 overflow-hidden rounded-xl border border-primary/15 bg-primary/5 p-3 sm:p-4"
      aria-hidden="true"
    >
      <div className="grid grid-cols-7 gap-1.5">
        {MATRIX_CELLS.map(({ index, delay, active }) => (
          <span
            key={index}
            className="relative aspect-square overflow-hidden rounded-[4px] border border-primary/10 bg-white/80"
          >
            <span
              className="qa-matrix-cell absolute inset-0 bg-primary"
              style={{ animationDelay: `${delay}s`, opacity: active ? 0.65 : 0.12 }}
            />
          </span>
        ))}
      </div>
      <div className="qa-matrix-scan pointer-events-none absolute inset-y-0 w-[18%] border-r border-primary/35 bg-gradient-to-r from-transparent to-primary/10" />
    </div>
  );
}

function QualityPreview() {
  return (
    <div className="relative mt-10 w-full max-w-xl">
      <div
        className="absolute -top-3 -left-3 size-9 border-t-2 border-l-2 border-primary/35"
        aria-hidden="true"
      />
      <div
        className="absolute -right-3 -bottom-3 size-9 border-r-2 border-b-2 border-primary/35"
        aria-hidden="true"
      />
      <div className="relative border border-primary/15 bg-white p-4 shadow-[0_24px_60px_-25px_rgba(20,65,110,.28)] sm:p-5">
        <div className="mb-5 flex items-center justify-between border-b border-primary/10 pb-4">
          <div className="flex items-center gap-2.5">
            <span className="flex size-8 items-center justify-center bg-primary text-primary-foreground">
              <CheckCheck className="size-4" />
            </span>
            <div className="text-left">
              <p className="text-xs font-bold text-slate-900">Quality overview</p>
              <p className="font-mono text-[10px] text-slate-500">PGI / WORKSPACE</p>
            </div>
          </div>
          <span className="font-mono text-[10px] font-semibold tracking-wider text-primary uppercase">
            See the full picture
          </span>
        </div>

        <div className="flex items-center gap-5 sm:gap-7">
          <QualityMatrix />
          <div className="min-w-0 flex-1 text-left">
            <div className="qa-seal mb-4 flex size-10 items-center justify-center rounded-full bg-primary/10 text-primary">
              <ShieldCheck className="size-5" strokeWidth={1.8} />
            </div>
            <p className="font-mono text-[10px] font-semibold tracking-[0.16em] text-primary uppercase">
              Quality signal
            </p>
            <p className="mt-1.5 text-base leading-tight font-bold tracking-tight text-slate-900 sm:text-lg">
              Know what needs attention.
            </p>
            <p className="mt-2 text-[11px] leading-relaxed text-slate-500">
              A clearer view from planning to resolution.
            </p>
          </div>
        </div>

        <div className="mt-5 grid grid-cols-3 border-t border-primary/10 pt-4">
          {CAPABILITIES.map(({ label, icon: Icon }) => (
            <div
              key={label}
              className="flex items-center justify-center gap-1.5 border-r border-primary/10 px-1 last:border-r-0"
            >
              <Icon className="size-3.5 shrink-0 text-primary" strokeWidth={1.8} />
              <span className="text-[10px] font-medium text-slate-600 sm:text-[11px]">{label}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export function AuthBranding() {
  return (
    <section className="relative hidden min-h-screen overflow-hidden border-l border-primary/10 bg-[#eef5ff] p-8 text-slate-900 select-none lg:col-span-7 lg:flex lg:flex-col lg:justify-between lg:p-12 xl:col-span-7">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 bg-[linear-gradient(to_right,rgba(20,80,150,.045)_1px,transparent_1px),linear-gradient(to_bottom,rgba(20,80,150,.045)_1px,transparent_1px)] bg-[size:32px_32px] [mask-image:linear-gradient(to_bottom,transparent,black_22%,black_80%,transparent)]"
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -top-32 -right-32 size-96 rounded-full border-[70px] border-primary/5"
      />

      <header className="relative z-10 flex items-center justify-end gap-4">
        <span className="hidden items-center gap-1 font-mono text-[10px] font-semibold tracking-wide text-primary uppercase xl:flex">
          Built for better releases <ArrowUpRight className="size-3.5" />
        </span>
      </header>

      <div className="relative z-10 my-auto flex flex-col items-center py-8 text-center">
        <span className="border-l-2 border-primary pl-3 font-mono text-[11px] font-semibold tracking-[0.18em] text-primary uppercase">
          From plan to proof
        </span>
        <h2 className="mt-5 max-w-xl text-4xl leading-[1.1] font-extrabold tracking-tight text-slate-900 xl:text-5xl">
          Make quality visible <span className="text-primary">at every step.</span>
        </h2>
        <p className="mt-4 max-w-lg text-sm leading-relaxed text-slate-600 xl:text-base">
          Trace requirements to sources, review each QA decision, and keep test planning grounded in
          evidence.
        </p>
        <QualityPreview />
      </div>

      <footer className="relative z-10 flex items-center justify-between gap-4 border-t border-primary/15 pt-5">
        <p className="font-mono text-[10px] font-semibold tracking-[0.15em] text-primary uppercase">
          Plan / Test / Improve
        </p>
        <p className="text-[11px] text-slate-500">One place for better quality decisions.</p>
      </footer>
    </section>
  );
}
