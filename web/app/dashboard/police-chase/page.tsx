import {createClient} from "@/lib/supabase/server";
import {AssetRepository} from "@/lib/vrf";
import {PoliceChaseRepository} from "@/lib/police-chase-repository";
import {PoliceChaseStudio} from "@/components/reels/PoliceChaseStudio";
export const dynamic="force-dynamic";
export default async function PoliceChasePage(){const supabase=await createClient();let assets:any[]=[];let projects:any[]=[];try{assets=(await new AssetRepository(supabase).listForUser()).filter(a=>Boolean(String(a.storage_path??"").trim()));projects=await new PoliceChaseRepository(supabase).list()}catch{}return <PoliceChaseStudio initialAssets={assets.map(a=>({id:a.id,title:a.title,duration_seconds:a.duration_seconds,storage_path:a.storage_path}))} initialProjects={projects}/>}
