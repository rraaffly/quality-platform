import React, { forwardRef } from "react";
import { cn } from "cn";

interface PageProps extends React.HTMLAttributes<HTMLDivElement> {}

export const AuthPage = forwardRef<HTMLDivElement, PageProps>(
  ({ children, className, ...rest }, ref) => {
    return (
      <div className={cn("w-full max-w-[420px]", className)} ref={ref} {...rest}>
        {children}
      </div>
    );
  },
);
