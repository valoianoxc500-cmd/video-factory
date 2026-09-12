"use client";
import type { Participant, ParticipantId } from "@/lib/fake-chat/types";
import styles from "./fake-chat.module.css";

export function ParticipantEditor({participant,onChange}:{participant:Participant;onChange:(value:Participant)=>void}){
  const update=(key:keyof Participant,value:Participant[keyof Participant])=>onChange({...participant,[key]:value});
  const image=(file:File|null)=>{if(!file)return;if(!file.type.startsWith("image/")){return}const reader=new FileReader();reader.onload=()=>update("avatar",String(reader.result||""));reader.readAsDataURL(file)};
  return <fieldset className={styles.participant}><legend>Person {participant.id}</legend><div className={styles.participantGrid}><label className={styles.avatarUpload}>{participant.avatar?<img src={participant.avatar} alt="Profile preview"/>:<span>{participant.name[0]?.toUpperCase()||participant.id}</span>}<input type="file" accept="image/*" onChange={(event)=>image(event.target.files?.[0]??null)}/><small>Photo</small></label><div className={styles.fields}><label>Name<input value={participant.name} onChange={(e)=>update("name",e.target.value)}/></label><label>Username<input value={participant.username} onChange={(e)=>update("username",e.target.value)}/></label></div></div><details><summary>More profile details</summary><div className={styles.fields}><label>Phone<input value={participant.phone} onChange={(e)=>update("phone",e.target.value)}/></label><label>Status<input value={participant.status} onChange={(e)=>update("status",e.target.value)}/></label><label>Last seen<input value={participant.lastSeen} onChange={(e)=>update("lastSeen",e.target.value)}/></label></div><div className={styles.checks}><label><input type="checkbox" checked={participant.activeNow} onChange={(e)=>update("activeNow",e.target.checked)}/>Active now</label><label><input type="checkbox" checked={participant.verified} onChange={(e)=>update("verified",e.target.checked)}/>Verified badge</label></div></details></fieldset>
}

export function swapParticipants<T extends Record<ParticipantId,Participant>>(people:T):T{return {A:{...people.B,id:"A"},B:{...people.A,id:"B"}} as T}
