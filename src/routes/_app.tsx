import { createFileRoute, Outlet } from "@tanstack/react-router";
import { protectPage } from "@/lib/auth";
import Layout from "@/components/layout";

export const Route = createFileRoute("/_app")({
  component: () => (
      <Layout>
        <Outlet />
      </Layout>
  ),
  beforeLoad: ({ location }) => {
    // All routes under /_app are protected
    protectPage(location);
  },
});
