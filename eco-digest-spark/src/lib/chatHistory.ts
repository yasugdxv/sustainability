import type { ChatMsg, Lang } from "@/lib/api";

// サスティナAIの会話履歴（ブラウザのlocalStorageのみに保存。バックエンドには送らない）
export interface ChatConversation {
  id: string;
  title: string;
  lang: Lang;
  messages: ChatMsg[];
  updatedAt: string;
}

const KEY = "verdant_chat_conversations";
const MAX_CONVERSATIONS = 50;

export function listConversations(): ChatConversation[] {
  try {
    const raw = localStorage.getItem(KEY);
    const list: ChatConversation[] = raw ? JSON.parse(raw) : [];
    return [...list].sort((a, b) => +new Date(b.updatedAt) - +new Date(a.updatedAt));
  } catch {
    return [];
  }
}

export function saveConversation(conv: ChatConversation): void {
  const list = listConversations().filter((c) => c.id !== conv.id);
  list.unshift(conv);
  try {
    localStorage.setItem(KEY, JSON.stringify(list.slice(0, MAX_CONVERSATIONS)));
  } catch {
    // localStorageが使えない/容量超過の場合は履歴保存を諦める（会話自体は継続できる）
  }
}

export function deleteConversation(id: string): void {
  const list = listConversations().filter((c) => c.id !== id);
  try {
    localStorage.setItem(KEY, JSON.stringify(list));
  } catch {
    // noop
  }
}

export function makeConversationId(): string {
  return `conv_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

export function titleFromMessages(messages: ChatMsg[]): string {
  const firstUser = messages.find((m) => m.role === "user");
  if (!firstUser) return "";
  const text = firstUser.content.trim();
  return text.length > 40 ? `${text.slice(0, 40)}…` : text;
}
