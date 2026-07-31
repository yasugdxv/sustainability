import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import { TopBar } from "@/components/top-bar";
import { AiChatPanel } from "@/components/ai-chat";
import { useLanguage } from "@/lib/i18n";
import type { ChatMsg, Lang } from "@/lib/api";
import {
  deleteConversation,
  listConversations,
  makeConversationId,
  saveConversation,
  titleFromMessages,
  type ChatConversation,
} from "@/lib/chatHistory";
import { Sparkles, Plus, MessageSquare, Trash2 } from "lucide-react";

export const Route = createFileRoute("/chat")({
  component: ChatPage,
});

function ChatPage() {
  const { lang, t } = useLanguage();
  const [conversations, setConversations] = useState<ChatConversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMsg[]>(() => [
    { role: "assistant", content: t("chat.seedMessage") },
  ]);
  // send()内の「質問→回答」2回のonMessagesChange呼び出しの間でReactの再レンダーが
  // 間に合わず、activeId(state)が古いクロージャのまま読まれて別々の会話IDが
  // 発行されてしまう問題を避けるため、同期的に読み書きできるrefで実体を管理する
  const activeConvRef = useRef<{ id: string; lang: Lang } | null>(null);

  useEffect(() => {
    setConversations(listConversations());
  }, []);

  const active = conversations.find((c) => c.id === activeId);

  const persist = (next: ChatMsg[]) => {
    setMessages(next);
    if (!next.some((m) => m.role === "user")) return; // 質問前の会話は保存しない
    if (!activeConvRef.current) {
      activeConvRef.current = { id: makeConversationId(), lang };
    }
    const { id, lang: convLang } = activeConvRef.current;
    const conv: ChatConversation = {
      id,
      title: titleFromMessages(next),
      lang: convLang,
      messages: next,
      updatedAt: new Date().toISOString(),
    };
    saveConversation(conv);
    setConversations(listConversations());
    setActiveId(id);
  };

  const startNew = () => {
    activeConvRef.current = null;
    setActiveId(null);
    setMessages([{ role: "assistant", content: t("chat.seedMessage") }]);
  };

  const openConversation = (conv: ChatConversation) => {
    activeConvRef.current = { id: conv.id, lang: conv.lang };
    setActiveId(conv.id);
    setMessages(conv.messages);
  };

  const removeConversation = (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    deleteConversation(id);
    const remaining = listConversations();
    setConversations(remaining);
    if (activeId === id) startNew();
  };

  return (
    <>
      <TopBar breadcrumb={[{ label: t("nav.sustainaAI") }]} />

      <div className="flex-1 grid grid-cols-[280px_1fr] min-h-0">
        <aside className="border-r border-border p-4 bg-muted/20 overflow-y-auto">
          <button
            onClick={startNew}
            className="w-full flex items-center gap-2 px-3 py-2.5 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:opacity-90"
          >
            <Plus className="h-4 w-4" /> {t("chat.newConversation")}
          </button>

          <div className="mt-6">
            <div className="text-[10px] uppercase tracking-[0.18em] text-muted-foreground mb-2 px-2">
              {t("chat.history")}
            </div>
            {conversations.length === 0 ? (
              <p className="text-xs text-muted-foreground px-2.5 leading-relaxed">
                {t("chat.historyEmpty")}
              </p>
            ) : (
              <div className="space-y-0.5">
                {conversations.map((c) => (
                  <button
                    key={c.id}
                    onClick={() => openConversation(c)}
                    className={`group w-full text-left px-2.5 py-2 rounded-md hover:bg-muted transition-colors ${
                      activeId === c.id ? "bg-muted" : ""
                    }`}
                  >
                    <div className="flex items-start gap-2">
                      <MessageSquare className="h-3.5 w-3.5 text-muted-foreground mt-0.5 shrink-0" />
                      <div className="min-w-0 flex-1">
                        <div className="text-sm truncate">{c.title}</div>
                        <div className="text-[10px] text-muted-foreground">
                          {new Date(c.updatedAt).toLocaleString(lang === "en" ? "en-US" : "ja-JP")}
                        </div>
                      </div>
                      <Trash2
                        className="h-3.5 w-3.5 text-muted-foreground opacity-0 group-hover:opacity-100 hover:text-destructive shrink-0"
                        onClick={(e) => removeConversation(c.id, e)}
                      />
                    </div>
                  </button>
                ))}
              </div>
            )}
          </div>

          <div className="mt-8 p-3 rounded-lg border border-border bg-card">
            <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-[0.18em] text-primary">
              <Sparkles className="h-3 w-3" /> {t("nav.sustainaAI")}
            </div>
            <p className="text-xs text-muted-foreground mt-2 leading-relaxed">
              {t("chat.footerDesc")}
            </p>
          </div>
        </aside>

        <div className="min-h-0">
          <AiChatPanel messages={messages} onMessagesChange={persist} sourceLang={active?.lang} />
        </div>
      </div>
    </>
  );
}
