import { redirect } from "next/navigation";
import { getUser } from "@/lib/supabase/server";

/** Signed-in users land in the dashboard; everyone else at sign-in. */
export default async function RootPage() {
  const user = await getUser();
  redirect(user ? "/dashboard" : "/login");
}
