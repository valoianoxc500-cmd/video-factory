import { createClient } from "@/lib/supabase/server";
import { FakeChatRepository } from "@/lib/fake-chat/repository";
import { FakeChatStudio } from "@/components/fake-chat/FakeChatStudio";
import type { FakeChatProject } from "@/lib/fake-chat/types";
export const dynamic="force-dynamic";
export default async function FakeChatPage(){let projects:FakeChatProject[]=[];try{const db=await createClient();const {data}=await db.auth.getUser();if(data.user)projects=await new FakeChatRepository(db,data.user.id).list()}catch{projects=[]}return <FakeChatStudio initialProjects={projects}/>}
