import ReactDOM from "react-dom/client";
import { RouterProvider, createRouter } from "@tanstack/react-router";

import "@/styles/globals.css";
import reportWebVitals from "./reportWebVitals.ts";
import { routeTree } from "./routeTree.gen.ts";
import { appConfig } from '@/lib/app-config';
import { reportError, trackPage, trackMetric } from '@/lib/telemetry';
import { queryClient } from '@/lib/query-client';
import { initializeTelemetry } from '@/lib/telemetry-providers';

void initializeTelemetry();

// Create a new router instance
const router = createRouter({
  routeTree,
  context: { queryClient },
  defaultPreload: "intent",
  scrollRestoration: true,
  defaultStructuralSharing: true,
  defaultPreloadStaleTime: 0,
});

document.title = appConfig.name;
// Route IDs avoid sending dynamic record IDs or URL search parameters to analytics.
router.subscribe('onResolved', () => {
  trackPage(router.state.matches.at(-1)?.routeId ?? '/');
});
window.addEventListener('error', (event) => reportError(event.error, 'window'));
window.addEventListener('unhandledrejection', (event) => reportError(event.reason, 'promise'));

// Register the router instance for type safety
declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
  interface StaticDataRouteOption {
    routeName?: string;
  }
}

// Render the app
const rootElement = document.getElementById("app");
if (rootElement && !rootElement.innerHTML) {
  const root = ReactDOM.createRoot(rootElement);
  root.render(<RouterProvider router={router} />);
}

reportWebVitals(trackMetric);
