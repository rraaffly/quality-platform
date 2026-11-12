import { createFileRoute } from '@tanstack/react-router'

export const Route = createFileRoute('/_app/workspace/')({
  component: WorkspaceIndexPage,
})

function WorkspaceIndexPage() {
  return <div>Hello "/_app/workspace/"!</div>
}
