"use client";
import { forwardRef } from "react";
import { presetById } from "@/lib/fake-chat/presets";
import { avatarInitials } from "@/lib/fake-chat/document";
import type { ChatMessage, FakeChatDocument, Participant } from "@/lib/fake-chat/types";
import styles from "./fake-chat.module.css";

const reactionGlyph:Record<string,string>={heart:"❤",laugh:"😂",thumbs_up:"👍",thumbs_down:"👎",fire:"🔥",surprise:"😮"};
const callLabel:Record<string,string>={missed_call:"Missed call",audio_call:"Audio call",video_call:"Video call"};
function Avatar({person}:{person:Participant}){return person.avatar?<img className={styles.avatar} src={person.avatar} alt=""/>:<span className={styles.avatarFallback}>{avatarInitials(person.name)}</span>}

function MessageContent({message}:{message:ChatMessage}){
  if(message.type==="image"||message.type==="video"||message.type==="sticker")return <><div className={`${styles.mediaMock} ${message.type==="sticker"?styles.sticker:""}`}>{message.media?<img src={message.media} alt="Uploaded message"/>:<span>{message.type==="video"?"▶ Video":"✦ Image"}</span>}{message.type==="video"&&<b>{message.duration}</b>}</div>{message.content&&<p>{message.content}</p>}</>;
  if(message.type==="voice")return <div className={styles.voice}><span>{message.played?"Ⅱ":"▶"}</span><i>{Array.from({length:18},(_,i)=><b key={i} style={{height:`${8+(i*7)%22}px`}}/> )}</i><small>{message.duration}</small></div>;
  if(message.type==="location")return <div className={styles.location}><span>⌖</span><div><b>{message.content||"Shared location"}</b><small>{message.duration||"Open in maps"}</small></div></div>;
  if(message.type in callLabel)return <div className={styles.call}><span>{message.type==="video_call"?"▣":"☎"}</span><div><b>{callLabel[message.type]}</b><small>{message.timestamp}</small></div></div>;
  return <p>{message.content}</p>;
}

export const PhonePreview=forwardRef<HTMLDivElement,{doc:FakeChatDocument}>(({doc},ref)=>{
  const preset=presetById(doc.preset);const person=doc.participants.B;const dark=doc.appearance.mode==="dark";
  const css={"--fc-sent":preset.variables.sent,"--fc-received":preset.variables.received,"--fc-accent":preset.variables.accent,"--fc-radius":preset.variables.radius,"--fc-font":preset.variables.font,"--fc-chat-bg":doc.appearance.backgroundColor} as React.CSSProperties;
  return <div className={`${styles.device} ${styles[doc.device.frame]} ${dark?styles.dark:""}`}>
    <div ref={ref} className={`${styles.screen} ${styles[`preset_${doc.preset}`]} ${styles[doc.appearance.spacing]}`} style={css} data-export-root>
      {doc.display.statusBar&&<div className={styles.statusBar}><b>{doc.device.time}</b>{doc.device.notch&&doc.device.frame!=="none"?<span className={styles.notch}/>:<span/>}<div><i>{"▮".repeat(doc.device.signal)}</i>{doc.device.wifi&&"⌁"}<em>{doc.device.battery}%</em></div></div>}
      <header className={styles.chatHead}><span className={styles.back}>‹</span><Avatar person={person}/><div><strong>{person.name}{person.verified&&preset.supportsVerified?<i className={styles.verified}>✓</i>:null}</strong><small>{doc.display.onlineStatus?(person.activeNow?"Active now":person.lastSeen||person.status):person.username}</small></div><span className={styles.headAction}>•••</span></header>
      <div className={styles.conversation} data-conversation style={doc.appearance.backgroundImage?{backgroundImage:`url(${doc.appearance.backgroundImage})`}:undefined}>
        {doc.display.dateSeparators&&<div className={styles.dateDivider}>{doc.display.dateLabel}</div>}
        {doc.messages.map((message,index)=>{
          if(message.type==="system")return <div className={styles.system} key={message.id}>{message.content}</div>;
          const mine=message.sender==="A",participant=doc.participants[message.sender],reply=doc.messages.find((item)=>item.id===message.replyTo),showAvatar=doc.display.avatars&&!mine&&(index===doc.messages.length-1||doc.messages[index+1]?.sender!==message.sender);
          return <div className={`${styles.row} ${mine?styles.mine:styles.theirs}`} key={message.id}>{!mine&&<span className={styles.avatarSlot}>{showAvatar?<Avatar person={participant}/>:null}</span>}<div className={styles.bubbleWrap}>{reply&&<div className={styles.reply}><b>{doc.participants[reply.sender].name}</b><span>{reply.content||reply.type}</span></div>}<div className={styles.bubble}><MessageContent message={message}/>{message.edited&&<small className={styles.edited}>edited</small>}</div>{message.reaction&&<span className={styles.reaction}>{reactionGlyph[message.reaction]}</span>}{doc.display.timestamps&&<small className={styles.meta}>{message.timestamp}{mine&&doc.display.readState&&message.seen?" · Seen":""}</small>}</div></div>})}
        {doc.display.typing!=="off"&&<div className={`${styles.row} ${doc.display.typing==="A"?styles.mine:styles.theirs}`}><div className={`${styles.bubble} ${styles.typing}`}><i/><i/><i/></div></div>}
      </div>
      <footer className={styles.composerMock}><span>＋</span><div>Message...</div><b>↑</b></footer>
    </div>
  </div>
});
PhonePreview.displayName="PhonePreview";
