import type {SupabaseClient} from "@supabase/supabase-js";
import type {TaskRow} from "./vrf";

const FIELDS="id, kind, status, payload, result, error, created_at, completed_at";
export class PoliceChaseRepository{
  constructor(private readonly db:SupabaseClient){}
  async list(limit=20):Promise<TaskRow[]>{const {data,error}=await this.db.from("vrf_tasks").select(FIELDS).eq("kind","process").contains("payload",{product:"police_chase"}).order("created_at",{ascending:false}).limit(Math.min(50,Math.max(1,limit)));if(error)throw new Error(error.message);return (data??[]) as unknown as TaskRow[]}
  async get(id:string):Promise<TaskRow>{const {data,error}=await this.db.from("vrf_tasks").select(FIELDS).eq("id",id).eq("kind","process").contains("payload",{product:"police_chase"}).maybeSingle();if(error)throw new Error(error.message);if(!data)throw new Error("That Police Chase project does not exist.");return data as unknown as TaskRow}
}
