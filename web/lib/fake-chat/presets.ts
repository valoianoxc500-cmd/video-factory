import type { ChatPresetId } from "./types";

export interface ChatPreset {
  id: ChatPresetId;
  name: string;
  mark: string;
  defaultMode: "light" | "dark";
  supportsVerified: boolean;
  supportsWallpaper: boolean;
  variables: Record<string, string>;
}

export const CHAT_PRESETS: ChatPreset[] = [
  { id:"ios",name:"iOS Messages",mark:"iM",defaultMode:"light",supportsVerified:false,supportsWallpaper:false,variables:{sent:"#0a84ff",received:"#e9e9eb",accent:"#0a84ff",radius:"20px",font:"-apple-system, BlinkMacSystemFont, 'Inter', sans-serif"}},
  { id:"instagram",name:"Instagram DM",mark:"IG",defaultMode:"light",supportsVerified:true,supportsWallpaper:false,variables:{sent:"linear-gradient(135deg,#8a3ffc,#df2673)",received:"#efeff0",accent:"#d62976",radius:"22px",font:"'Inter', sans-serif"}},
  { id:"whatsapp",name:"WhatsApp",mark:"WA",defaultMode:"light",supportsVerified:false,supportsWallpaper:true,variables:{sent:"#d9fdd3",received:"#ffffff",accent:"#00a884",radius:"10px",font:"'Inter', sans-serif"}},
  { id:"messenger",name:"Messenger",mark:"MS",defaultMode:"light",supportsVerified:true,supportsWallpaper:false,variables:{sent:"#1687ff",received:"#e4e6eb",accent:"#1687ff",radius:"20px",font:"'Inter', sans-serif"}},
  { id:"snapchat",name:"Snapchat Chat",mark:"SC",defaultMode:"light",supportsVerified:false,supportsWallpaper:false,variables:{sent:"#f2f2f2",received:"#ffffff",accent:"#ff2d8d",radius:"4px",font:"'Inter', sans-serif"}},
  { id:"x",name:"X / Twitter DM",mark:"X",defaultMode:"dark",supportsVerified:true,supportsWallpaper:false,variables:{sent:"#1d9bf0",received:"#2f3336",accent:"#1d9bf0",radius:"20px",font:"'Inter', sans-serif"}},
  { id:"dark",name:"Generic Dark DM",mark:"DM",defaultMode:"dark",supportsVerified:true,supportsWallpaper:true,variables:{sent:"#6d5dfc",received:"#272936",accent:"#a89cff",radius:"18px",font:"'Plus Jakarta Sans', sans-serif"}},
  { id:"light",name:"Generic Light Chat",mark:"CH",defaultMode:"light",supportsVerified:true,supportsWallpaper:true,variables:{sent:"#191b22",received:"#eef0f4",accent:"#191b22",radius:"18px",font:"'Plus Jakarta Sans', sans-serif"}},
];

export function presetById(id: unknown): ChatPreset {
  return CHAT_PRESETS.find((preset) => preset.id === id) ?? CHAT_PRESETS[0];
}
