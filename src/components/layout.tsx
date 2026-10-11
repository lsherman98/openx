import { AppSidebar } from "@/components/app-sidebar";
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar";

import type { PropsWithChildren } from "react";
import { AppHeader } from "./header/app-header";

export default function Layout({ children }: PropsWithChildren) {
  return (
    <SidebarProvider
      style={
        {
          "--sidebar-width": "calc(var(--spacing) * 72)",
          "--header-height": "calc(var(--spacing) * 12)",
        } as React.CSSProperties
      }
      className={
        "text-foreground group/body overscroll-none font-sans antialiased [--footer-height:calc(var(--spacing)*14)] [--header-height:calc(var(--spacing)*14)] xl:[--footer-height:calc(var(--spacing)*24)]"
      }
      defaultOpen={true}
    >
      <AppSidebar variant="inset" />
      <SidebarInset className="overflow-hidden">
        <AppHeader />
        <div className="flex flex-1 p-4">{children}</div>
      </SidebarInset>
    </SidebarProvider>
  );
}
