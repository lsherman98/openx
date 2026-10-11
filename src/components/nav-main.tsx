import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar";
import { Badge } from "@/components/ui/badge";
import { Link } from "@tanstack/react-router";
import type { LucideIcon } from "lucide-react";

export function NavMain({
  items,
}: {
  items: {
    title: string;
    url: string;
    icon?: LucideIcon;
    disabled?: boolean;
    badge?: {
      text: string;
      url: string;
    };
  }[];
}) {
  return (
    <SidebarGroup>
      <SidebarGroupContent className="flex flex-col gap-2">
        <SidebarMenu>
          {items.map((item) => (
            <SidebarMenuItem key={item.title}>
              {item.disabled && item.badge ? (
                <SidebarMenuButton className="cursor-not-allowed">
                  {item.icon && <item.icon />}
                  <span className="opacity-50">{item.title}</span>
                  <Link to={item.badge.url} className="ml-auto">
                    <Badge variant={"outline"} asChild>
                      <span className="cursor-pointer hover:opacity-80">{item.badge.text}</span>
                    </Badge>
                  </Link>
                </SidebarMenuButton>
              ) : item.disabled ? (
                <SidebarMenuButton className="cursor-not-allowed">
                  {item.icon && <item.icon />}
                  <span className="opacity-50">{item.title}</span>
                </SidebarMenuButton>
              ) : (
                <Link to={item.url}>
                  <SidebarMenuButton className="cursor-pointer">
                    {item.icon && <item.icon />}
                    <span>{item.title}</span>
                  </SidebarMenuButton>
                </Link>
              )}
            </SidebarMenuItem>
          ))}
        </SidebarMenu>
      </SidebarGroupContent>
    </SidebarGroup>
  );
}
