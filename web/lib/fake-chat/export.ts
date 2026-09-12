export type ExportMode="screen"|"story"|"native"|"full";

const dimensions:Record<Exclude<ExportMode,"full">,[number,number]>={screen:[860,1720],story:[1080,1920],native:[860,1720]};

function inlineComputedStyles(source:Element,target:Element){
  const style=getComputedStyle(source);(target as HTMLElement).style.cssText=Array.from(style).map((key)=>`${key}:${style.getPropertyValue(key)};`).join("");
  Array.from(source.children).forEach((child,index)=>inlineComputedStyles(child,target.children[index]));
}

export async function exportChatPng(element:HTMLElement,mode:ExportMode,filename="fake-chat.png"):Promise<Blob>{
  await document.fonts?.ready;
  const full=mode==="full";const sourceWidth=element.clientWidth;const conversation=element.querySelector<HTMLElement>("[data-conversation]");const sourceHeight=full&&conversation?element.clientHeight-conversation.clientHeight+conversation.scrollHeight:element.clientHeight;
  if(!sourceWidth||!sourceHeight)throw new Error("We couldn't export the image. Try again.");
  const clone=element.cloneNode(true) as HTMLElement;inlineComputedStyles(element,clone);clone.style.width=`${sourceWidth}px`;clone.style.height=`${sourceHeight}px`;clone.style.overflow="hidden";const clonedConversation=clone.querySelector<HTMLElement>("[data-conversation]");if(conversation&&clonedConversation){if(full){clonedConversation.style.height=`${conversation.scrollHeight}px`;clonedConversation.style.overflow="visible";clonedConversation.style.flex="none"}else if(conversation.scrollTop){Array.from(clonedConversation.children).forEach((child)=>{(child as HTMLElement).style.transform=`translateY(-${conversation.scrollTop}px)`})}}
  const svg=`<svg xmlns="http://www.w3.org/2000/svg" width="${sourceWidth}" height="${sourceHeight}"><foreignObject width="100%" height="100%">${new XMLSerializer().serializeToString(clone)}</foreignObject></svg>`;
  const image=new Image();const url=URL.createObjectURL(new Blob([svg],{type:"image/svg+xml;charset=utf-8"}));
  try{await new Promise<void>((resolve,reject)=>{image.onload=()=>resolve();image.onerror=()=>reject(new Error("We couldn't export the image. Try again."));image.src=url});
    const [width,height]=full?[sourceWidth*2,sourceHeight*2]:dimensions[mode];const canvas=document.createElement("canvas");canvas.width=width;canvas.height=height;const ctx=canvas.getContext("2d");if(!ctx)throw new Error("We couldn't export the image. Try again.");ctx.drawImage(image,0,0,width,height);
    const blob=await new Promise<Blob|null>((resolve)=>canvas.toBlob(resolve,"image/png",1));if(!blob)throw new Error("We couldn't export the image. Try again.");
    const link=document.createElement("a");const downloadUrl=URL.createObjectURL(blob);link.href=downloadUrl;link.download=filename;link.click();setTimeout(()=>URL.revokeObjectURL(downloadUrl),1000);return blob;
  }finally{URL.revokeObjectURL(url)}
}
