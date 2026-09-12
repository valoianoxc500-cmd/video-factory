import { CHAT_PRESETS, presetById } from "./presets";
import type { ChatMessage, FakeChatDocument, MessageType, ParticipantId, Reaction } from "./types";

const TYPES = new Set<MessageType>(["text","image","voice","video","sticker","location","missed_call","audio_call","video_call","system"]);
const REACTIONS = new Set<Reaction>(["","heart","laugh","thumbs_up","thumbs_down","fire","surprise"]);
const id = () => globalThis.crypto?.randomUUID?.() ?? `m_${Date.now()}_${Math.random().toString(36).slice(2)}`;
export const avatarInitials=(name:string)=>String(name||"").trim().split(/\s+/).slice(0,2).map((part)=>part[0]?.toUpperCase()).join("")||"?";

export function newMessage(sender: ParticipantId="A", content="", type: MessageType="text"): ChatMessage {
  return { id:id(),sender,type,content,media:"",timestamp:"10:42",reaction:"",seen:sender==="A",replyTo:null,edited:false,duration:"0:18",played:false };
}

export function newDocument(): FakeChatDocument {
  return {
    version:1,title:"Untitled chat",preset:"ios",
    participants:{
      A:{id:"A",name:"Alex",username:"@alex",phone:"",avatar:"",status:"Available",activeNow:true,lastSeen:"",verified:false},
      B:{id:"B",name:"Jamie",username:"@jamie",phone:"",avatar:"",status:"Active now",activeNow:true,lastSeen:"",verified:false},
    },
    messages:[
      {...newMessage("B","You are not going to believe what just happened.")},
      {...newMessage("A","Okay, now you have my attention 👀"),timestamp:"10:43"},
      {...newMessage("B","Remember that impossible idea we talked about? It worked."),timestamp:"10:43",reaction:"fire"},
    ],
    appearance:{mode:"light",backgroundColor:"#ffffff",backgroundImage:"",spacing:"normal"},
    device:{frame:"iphone",time:"9:41",battery:82,signal:4,wifi:true,notch:true},
    display:{statusBar:true,timestamps:true,avatars:true,readState:true,dateSeparators:true,dateLabel:"Today",typing:"off",onlineStatus:true},
  };
}

const templates: Record<string, [string,string,ParticipantId][]> = {
  blank:[], dating:[["I had a really good time tonight.","10:18","B"],["Same. I was hoping you'd say that.","10:19","A"]],
  funny:[["I tried your cooking advice.","12:04","B"],["And?","12:04","A"],["The smoke alarm loved it.","12:05","B"]],
  argument:[["Can we talk about what happened?","18:31","A"],["Yes. But honestly this time.","18:33","B"]],
  business:[["The client approved the direction.","09:12","B"],["Perfect. Sending the final files now.","09:13","A"]],
  support:[["Hi, I need help with my order.","14:02","B"],["I'm here to help. What went wrong?","14:03","A"]],
};

export function documentFromTemplate(name: string): FakeChatDocument {
  const doc=newDocument();
  doc.title=name==="blank"?"Untitled chat":`${name[0].toUpperCase()}${name.slice(1)} chat`;
  doc.messages=(templates[name]??templates.blank).map(([content,timestamp,sender])=>({...newMessage(sender,content),timestamp}));
  return doc;
}

export function normaliseDocument(raw: unknown): FakeChatDocument {
  const fallback=newDocument();
  if(!raw||typeof raw!=="object") return fallback;
  const row=raw as Partial<FakeChatDocument>;
  const doc:FakeChatDocument={...fallback,...row,version:1,preset:presetById(row.preset).id,
    participants:{A:normaliseParticipant(row.participants?.A,fallback.participants.A,"A"),B:normaliseParticipant(row.participants?.B,fallback.participants.B,"B")},
    appearance:{...fallback.appearance,...(row.appearance??{})},device:{...fallback.device,...(row.device??{})},display:{...fallback.display,...(row.display??{})},
    messages:Array.isArray(row.messages)?row.messages.slice(0,500).map((message,index)=>normaliseMessage(message,index)):fallback.messages};
  const ids=new Set(doc.messages.map((message)=>message.id));
  doc.messages=doc.messages.map((message)=>({...message,replyTo:message.replyTo&&ids.has(message.replyTo)?message.replyTo:null}));
  doc.device.battery=Math.max(0,Math.min(100,Number(doc.device.battery)||0));
  doc.device.signal=Math.max(0,Math.min(4,Number(doc.device.signal)||0));
  return doc;
}

function normaliseParticipant(raw:unknown,fallback:FakeChatDocument["participants"]["A"],participantId:ParticipantId){const row=(raw&&typeof raw==="object"?raw:{}) as Partial<typeof fallback>;return {...fallback,...row,id:participantId,name:String(row.name??fallback.name).slice(0,80),username:String(row.username??fallback.username).slice(0,80),phone:String(row.phone??"").slice(0,40),avatar:String(row.avatar??"").slice(0,2_000_000),status:String(row.status??"").slice(0,120),lastSeen:String(row.lastSeen??"").slice(0,80),activeNow:Boolean(row.activeNow),verified:Boolean(row.verified)}}

function normaliseMessage(raw: unknown,index: number):ChatMessage {
  const row=(raw&&typeof raw==="object"?raw:{}) as Partial<ChatMessage>;
  return {id:String(row.id||`legacy_${index}_${id()}`),sender:row.sender==="B"?"B":"A",type:TYPES.has(row.type as MessageType)?row.type as MessageType:"text",content:String(row.content??"").slice(0,4000),media:String(row.media??"").slice(0,2_000_000),timestamp:String(row.timestamp??""),reaction:REACTIONS.has(row.reaction as Reaction)?row.reaction as Reaction:"",seen:Boolean(row.seen),replyTo:row.replyTo?String(row.replyTo):null,edited:Boolean(row.edited),duration:String(row.duration??"0:18").slice(0,10),played:Boolean(row.played)};
}

export function updateMessage(doc:FakeChatDocument,message:ChatMessage):FakeChatDocument{return {...doc,messages:doc.messages.map((item)=>item.id===message.id?message:item)}}
export function deleteMessage(doc:FakeChatDocument,messageId:string):FakeChatDocument{return {...doc,messages:doc.messages.filter((item)=>item.id!==messageId).map((item)=>item.replyTo===messageId?{...item,replyTo:null}:item)}}
export function duplicateMessage(doc:FakeChatDocument,messageId:string):FakeChatDocument{const index=doc.messages.findIndex((item)=>item.id===messageId);if(index<0)return doc;const messages=[...doc.messages];messages.splice(index+1,0,{...doc.messages[index],id:id(),replyTo:null});return {...doc,messages}}
export function moveMessage(doc:FakeChatDocument,messageId:string,direction:-1|1):FakeChatDocument{const from=doc.messages.findIndex((item)=>item.id===messageId),to=from+direction;if(from<0||to<0||to>=doc.messages.length)return doc;const messages=[...doc.messages];[messages[from],messages[to]]=[messages[to],messages[from]];return {...doc,messages}}
export function switchPreset(doc:FakeChatDocument,presetId:unknown):FakeChatDocument{const preset=presetById(presetId);return {...doc,preset:preset.id,appearance:{...doc.appearance,mode:preset.defaultMode}}}
export const TEMPLATE_NAMES=Object.keys(templates);
export { CHAT_PRESETS };
