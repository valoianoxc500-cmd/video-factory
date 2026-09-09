import { redirect } from "next/navigation";
import { getUser } from "@/lib/supabase/server";
import { Sidebar } from "@/components/Sidebar";

/**
 * Dashboard frame.
 *
 * Every page under /dashboard renders inside this, and it refuses to render at
 * all without a verified session. Middleware also redirects, but the check is
 * repeated here because a layout must not depend on middleware having run.
 */
export default async function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const user = await getUser();
  if (!user) redirect("/login");

  // The sidebar no longer lists channels -- the Dashboard does, with counts --
  // so the shell does not need to load them on every page.
  return (
    <div className="dash">
      <Sidebar email={user.email ?? ""} />
      <main className="content" id="main-content">{children}</main>
    </div>
  );
}
