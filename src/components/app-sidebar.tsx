import { NavMain } from "@/components/nav-main";
import { NavUser } from "@/components/nav-user";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuItem,
} from "@/components/ui/sidebar";
import { Home } from "lucide-react";
import { pb } from "@/lib/pocketbase";
import { Link } from "@tanstack/react-router";
import { appConfig } from '@/lib/app-config';

export function AppSidebar({ ...props }: React.ComponentProps<typeof Sidebar>) {
  const data = {
    navMain: [
      {
        title: "Home",
        url: "/",
        icon: Home,
      },
    ],
  };

  const user = pb.authStore.record;
  return (
    <Sidebar collapsible="offcanvas" {...props}>
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <Link to="/" className="flex items-center">
              {appConfig.name}
            </Link>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>
      <SidebarContent>
        <NavMain items={data.navMain} />
      </SidebarContent>
      <SidebarFooter>
        <NavUser email={user?.email ?? ''} />
      </SidebarFooter>
    </Sidebar>
  );
}
