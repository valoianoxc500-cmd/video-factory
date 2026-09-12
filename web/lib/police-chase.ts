export const CHASE_LENGTHS=[15,30,45,60,90] as const;
export const CHASE_COUNTS=[1,2,3,4,5] as const;
export const CAPTION_LANGUAGES=[{id:"auto",label:"Auto"},{id:"en",label:"English"},{id:"ar",label:"Arabic"},{id:"none",label:"None"}] as const;
export const CTA_MODES=[{id:"auto",label:"Auto"},{id:"custom",label:"Custom"},{id:"off",label:"Off"}] as const;
export type ChaseOptions={targetSeconds:number;count:number;captionLanguage:string;ctaMode:string;ctaText:string;ctaPlacement:"end"|"persistent"};
export type ChaseSource={title:string;sourceAgency:string;originalSourceUrl:string;footageDate:string;location:string;reuseBasis:string;attributionRequirement:string;verificationStatus:string;notes:string};
export type ChaseClip={id:string;title:string;start:number;end:number;duration:number;viral_score:number;reason:string;status:"done"|"skipped"|"failed";caption_status:string;cta_text:string;path?:string;error?:string};
export const defaultChaseOptions=():ChaseOptions=>({targetSeconds:30,count:1,captionLanguage:"auto",ctaMode:"auto",ctaText:"",ctaPlacement:"end"});
export const defaultChaseSource=():ChaseSource=>({title:"",sourceAgency:"",originalSourceUrl:"",footageDate:"",location:"",reuseBasis:"owned_or_permitted",attributionRequirement:"",verificationStatus:"user_attested",notes:""});
export function safeChaseError(raw:unknown){const value=String(raw??"").trim();return !value||/ffmpeg|traceback|provider|subprocess|stderr|[a-z]:\\|\/tmp\//i.test(value)||value.length>200?"This project needs attention. Your completed clips are safe.":value}
export function reuseLabel(status:string){return status==="verified_reusable"?"Verified reusable":status==="user_attested"?"Authorized by you":"Reuse status unclear"}
