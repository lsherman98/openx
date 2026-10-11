import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { createRootRouteWithContext, Link, Outlet } from "@tanstack/react-router";

import { TooltipProvider } from "@/components/ui/tooltip";

import { queryClient } from '@/lib/query-client';
import { ThemeProvider } from '@/components/theme-provider';
import { Toaster } from '@/components/ui/sonner';
import { ErrorPage } from '@/components/error-page';

export const Route = createRootRouteWithContext<{ queryClient: QueryClient }>()({
  errorComponent: ErrorPage,
  notFoundComponent: () => <main className="p-6"><h1 className="text-xl font-semibold">Page not found</h1><Link to="/" className="text-sm underline">Go home</Link></main>,
  component: () => (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider defaultTheme="system" enableSystem>
        <TooltipProvider>
        <Outlet />
        <Toaster position="bottom-right" />
        </TooltipProvider>
      </ThemeProvider>
    </QueryClientProvider>
  ),
});
