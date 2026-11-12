import { AuthBranding } from '#/components/layout'
import { createFileRoute, Outlet } from '@tanstack/react-router'
import {ShieldCheck} from "lucide-react"

export const Route = createFileRoute('/_auth')({
  component: AuthLayout,
})

function AuthLayout() {
  return (
    <div className="grid min-h-screen w-full grid-cols-1 bg-background font-sans text-slate-800 lg:grid-cols-12">
      <div className="relative z-10 col-span-1 flex min-h-screen flex-col justify-between bg-white p-6 sm:p-10 lg:col-span-5 lg:p-12 xl:col-span-5">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-primary text-white">
            <ShieldCheck className="size-5" />
          </div>
          <div>
            <h1 className="text-xl leading-tight font-extrabold tracking-tight text-slate-900">PGI Quality Platform</h1>
            <p className="text-xs font-medium text-slate-500">Quality Assurance Test Platform</p>
          </div>
        </div>

        <Outlet />

        <div className="flex flex-col items-center justify-center gap-2 border-t border-slate-100 pt-6 text-xs text-slate-400 sm:flex-row">
          <span>
            &copy; 2026 <span className="font-medium">PT. Pasifik Global Integrasi</span>. All rights reserved.
          </span>
        </div>
      </div>

      <AuthBranding />
    </div>

  )
}
