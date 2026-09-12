import type { SupabaseClient } from "@supabase/supabase-js";
import { normaliseDocument } from "./document";
import type { FakeChatDocument, FakeChatProject } from "./types";

type Row={id:string;title:string;document:unknown;created_at:string;updated_at:string};
const fields="id, title, document, created_at, updated_at";
const project=(row:Row):FakeChatProject=>({id:row.id,title:String(row.title||"Untitled chat"),document:normaliseDocument(row.document),createdAt:row.created_at,updatedAt:row.updated_at});
export class FakeChatRepository{
  constructor(private db:SupabaseClient,private userId:string){}
  async list(limit=30){const {data,error}=await this.db.from("fake_chat_projects").select(fields).order("updated_at",{ascending:false}).limit(Math.min(50,Math.max(1,limit)));if(error)throw new Error(error.message);return ((data??[]) as Row[]).map(project)}
  async get(id:string){const {data,error}=await this.db.from("fake_chat_projects").select(fields).eq("id",id).maybeSingle();if(error)throw new Error(error.message);if(!data)throw new Error("That chat does not exist.");return project(data as Row)}
  async create(title:string,document:FakeChatDocument){const clean=normaliseDocument(document);const {data,error}=await this.db.from("fake_chat_projects").insert({user_id:this.userId,title:String(title||clean.title).trim().slice(0,120)||"Untitled chat",document:clean}).select(fields).single();if(error)throw new Error(error.message);return project(data as Row)}
  async update(id:string,title:string,document:FakeChatDocument){const {data,error}=await this.db.from("fake_chat_projects").update({title:String(title).trim().slice(0,120)||"Untitled chat",document:normaliseDocument(document),updated_at:new Date().toISOString()}).eq("id",id).select(fields).maybeSingle();if(error)throw new Error(error.message);if(!data)throw new Error("That chat does not exist.");return project(data as Row)}
  async remove(id:string){const {error}=await this.db.from("fake_chat_projects").delete().eq("id",id);if(error)throw new Error(error.message)}
}
