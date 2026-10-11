import { Separator } from "@/components/ui/separator";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { useLocation, useMatches } from "@tanstack/react-router";

export function AppHeader() {
  const route = useLocation();
  const matches = useMatches();
  const match = matches.find((m) => m.pathname === route.pathname + "/" || m.pathname === route.pathname);

  return (
    <header className="flex h-(--header-height) shrink-0 items-center gap-2 border-b border-border transition-[width,height] ease-linear group-has-data-[collapsible=icon]/sidebar-wrapper:h-(--header-height)">
      <div className="flex w-full items-center gap-1 px-4 lg:gap-2 lg:px-6 relative">
        <SidebarTrigger className="-ml-1" />
        <Separator orientation="vertical" className="mx-2 data-[orientation=vertical]:h-4" />
        <h1 className="text-base font-medium">{match?.staticData.routeName}</h1>
      </div>
    </header>
  );
}
