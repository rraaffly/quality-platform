import React, { forwardRef } from 'react'
import { cn } from 'cn'

interface PageProps extends React.HTMLAttributes<HTMLDivElement> {}

export const AuthPage = forwardRef<HTMLDivElement, PageProps>(({ children, className, ...rest }, ref) => {
  return (
    <div className={cn('mx-auto my-auto w-full max-w-md py-8', className)} ref={ref} {...rest}>
      {children}
    </div>
  )
})
