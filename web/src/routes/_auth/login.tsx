import { AuthPage } from '#/components/layout'
import { LoginForm } from '#/features/auth'
import { createFileRoute } from '@tanstack/react-router'

export const Route = createFileRoute('/_auth/login')({
  component: LoginPage,
})

function LoginPage() {
  return (
    <AuthPage id="login">
      <div className="w-full space-y-6">
        <div className="space-y-1.5 text-left">
          <h2 className="text-2xl font-bold tracking-tight text-foreground">
            Welcome Back!
          </h2>
          <p className="text-sm text-muted-foreground">
            Enter your email address and password to access.
          </p>
        </div>

        <LoginForm />
      </div>
    </AuthPage>

  )
}
