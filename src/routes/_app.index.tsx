import { createFileRoute } from '@tanstack/react-router'
import { appConfig } from '@/lib/app-config'

export const Route = createFileRoute('/_app/')({
  staticData: { routeName: 'Home' },
  component: Home,
})

function Home() {
  return <div className="p-6"><h2 className="text-xl font-semibold">{appConfig.name}</h2><p className="mt-2 text-sm text-muted-foreground">Your application is ready. Start building here.</p></div>
}
