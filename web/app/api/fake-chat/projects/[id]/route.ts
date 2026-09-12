import { createClient,requireUser } from "@/lib/supabase/server";
import { FakeChatRepository } from "@/lib/fake-chat/repository";
import { normaliseDocument } from "@/lib/fake-chat/document";
export const dynamic="force-dynamic";
const fail=(error:unknown,status=500)=>{console.error("[fake-chat] project operation failed",error instanceof Error?error.name:"unknown");return Response.json({error:status===404?"That chat does not exist.":"We couldn't save this chat."},{status})};
export async function GET(_request:Request,{params}:{params:Promise<{id:string}>}){try{const {id}=await params;const user=await requireUser();const db=await createClient();return Response.json({project:await new FakeChatRepository(db,user.id).get(id)})}catch(error){return fail(error,404)}}
export async function PATCH(request:Request,{params}:{params:Promise<{id:string}>}){try{const {id}=await params;const user=await requireUser();const body=await request.json() as Record<string,unknown>;const db=await createClient();const project=await new FakeChatRepository(db,user.id).update(id,String(body.title||"Untitled chat"),normaliseDocument(body.document));return Response.json({project})}catch(error){return fail(error)}}
export async function DELETE(_request:Request,{params}:{params:Promise<{id:string}>}){try{const {id}=await params;const user=await requireUser();const db=await createClient();await new FakeChatRepository(db,user.id).remove(id);return new Response(null,{status:204})}catch(error){return fail(error)}}
