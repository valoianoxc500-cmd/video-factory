export type ParticipantId = "A" | "B";
export type ChatPresetId = "ios" | "instagram" | "whatsapp" | "messenger" | "snapchat" | "x" | "dark" | "light";
export type MessageType = "text" | "image" | "voice" | "video" | "sticker" | "location" | "missed_call" | "audio_call" | "video_call" | "system";
export type Reaction = "heart" | "laugh" | "thumbs_up" | "thumbs_down" | "fire" | "surprise" | "";

export interface Participant {
  id: ParticipantId;
  name: string;
  username: string;
  phone: string;
  avatar: string;
  status: string;
  activeNow: boolean;
  lastSeen: string;
  verified: boolean;
}

export interface ChatMessage {
  id: string;
  sender: ParticipantId;
  type: MessageType;
  content: string;
  media: string;
  timestamp: string;
  reaction: Reaction;
  seen: boolean;
  replyTo: string | null;
  edited: boolean;
  duration: string;
  played: boolean;
}

export interface FakeChatDocument {
  version: 1;
  title: string;
  preset: ChatPresetId;
  participants: Record<ParticipantId, Participant>;
  messages: ChatMessage[];
  appearance: { mode: "light" | "dark"; backgroundColor: string; backgroundImage: string; spacing: "normal" | "compact" };
  device: { frame: "iphone" | "android" | "none"; time: string; battery: number; signal: number; wifi: boolean; notch: boolean };
  display: { statusBar: boolean; timestamps: boolean; avatars: boolean; readState: boolean; dateSeparators: boolean; dateLabel: string; typing: ParticipantId | "off"; onlineStatus: boolean };
}

export interface FakeChatProject {
  id: string;
  title: string;
  document: FakeChatDocument;
  createdAt: string;
  updatedAt: string;
}
