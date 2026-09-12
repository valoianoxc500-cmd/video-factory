import { createClient,requireUser } from "@/lib/supabase/server";
import { FakeChatRepository } from "@/lib/fake-chat/repository";
import { normaliseDocument } from "@/lib/fake-chat/document";
export const dynamic="force-dynamic";
const fail=(error:unknown)=>{console.error("[fake-chat] project operation failed",error instanceof Error?error.name:"unknown");return Response.json({error:"We couldn't save this chat."},{status:500})};
export async function GET(){try{const user=await requireUser();const db=await createClient();return Response.json({projects:await new FakeChatRepository(db,user.id).list()})}catch(error){return fail(error)}}
export async function POST(request:Request){try{const user=await requireUser();const body=await request.json() as Record<string,unknown>;const doc=normaliseDocument(body.document);const db=await createClient();const project=await new FakeChatRepository(db,user.id).create(String(body.title||doc.title),doc);return Response.json({project},{status:201})}catch(error){return fail(error)}}
