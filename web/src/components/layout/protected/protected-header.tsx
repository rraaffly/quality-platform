import { Link } from '@tanstack/react-router'
import { ShieldCheck } from 'lucide-react'

export function ProtectedHeader() {
  return (
    <header className="relative z-30 flex h-10 w-full shrink-0 items-center justify-between border-b border-border bg-card px-3 text-foreground select-none sm:px-4">
      <div className="flex items-center gap-3">
        <Link to="/" className="group flex items-center gap-2">
          <div className="flex h-6 w-6 items-center justify-center bg-primary text-primary-foreground shadow-2xs">
            <ShieldCheck className="size-3.5" />
          </div>
          <span className="text-sm font-extrabold tracking-tight text-foreground">PGI Quality Platform</span>
        </Link>
      </div>

      <div className="mx-4 hidden max-w-sm flex-1 items-center justify-center md:flex">
        <span className="text-xs">Breadcrumb</span>
      </div>

      <div className="flex items-center gap-2">
        {/* <NotificationPopover /> */}
      </div>
    </header>
  )
}