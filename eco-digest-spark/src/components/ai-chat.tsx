import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { Sparkles, Send, X, Maximize2, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { useChatSend, useTranslateTexts, type ChatMsg, type Lang } from "@/lib/api";
import { chatSuggestions, useLanguage } from "@/lib/i18n";

export function AiChatPanel({
  compact = false,
  onClose,
  messages: controlledMessages,
  onMessagesChange,
  sourceLang,
}: {
  compact?: boolean;
  onClose?: () => void;
  /** 指定すると会話状態はこのコンポーネントの外（呼び出し元）で管理される（履歴保存用） */
  messages?: ChatMsg[];
  onMessagesChange?: (messages: ChatMsg[]) => void;
  /** この会話が最初に行われた言語。現在の表示言語と異なる場合のみ表示用に翻訳する */
  sourceLang?: Lang;
}) {
  const { lang, t } = useLanguage();
  const [internalMessages, setInternalMessages] = useState<ChatMsg[]>(() => [
    { role: "assistant", content: t("chat.seedMessage") },
  ]);
  const messages = controlledMessages ?? internalMessages;
  const [input, setInput] = useState("");
  const chatSend = useChatSend();

  const needsTranslation = !!sourceLang && sourceLang !== lang;
  const { data: translated, isFetching: isTranslating } = useTranslateTexts(
    needsTranslation ? messages.map((m) => m.content) : [],
    lang,
    needsTranslation,
  );
  const displayMessages: ChatMsg[] =
    needsTranslation && translated
      ? messages.map((m, i) => ({ ...m, content: translated.translations[i] ?? m.content }))
      : messages;

  const updateMessages = (next: ChatMsg[]) => {
    if (onMessagesChange) onMessagesChange(next);
    else setInternalMessages(next);
  };

  const send = (text: string) => {
    if (!text.trim() || chatSend.isPending) return;
    const history = messages; // 保存・送信は常に原文ベース（表示用の翻訳とは分離する）
    updateMessages([...history, { role: "user", content: text }]);
    setInput("");

    chatSend.mutate(
      { message: text, history, lang },
      {
        onSuccess: (data) => {
          updateMessages([
            ...history,
            { role: "user", content: text },
            { role: "assistant", content: data.reply, sources: data.sources },
          ]);
        },
        onError: (err: Error) => {
          updateMessages([
            ...history,
            { role: "user", content: text },
            {
              role: "assistant",
              content: `${lang === "en" ? "An error occurred" : "エラーが発生しました"}: ${err.message}`,
            },
          ]);
        },
      },
    );
  };

  return (
    <div className="flex h-full flex-col bg-card">
      <header className="flex items-center justify-between border-b border-border px-5 py-3.5">
        <div className="flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-md bg-primary text-primary-foreground">
            <Sparkles className="h-4 w-4" />
          </div>
          <div>
            <div className="text-editorial text-lg leading-none">{t("nav.sustainaAI")}</div>
            <div className="text-[10px] uppercase tracking-[0.18em] text-muted-foreground mt-1">
              {t("chat.headerSubtitle")}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1">
          {needsTranslation && isTranslating && (
            <span className="text-[10px] text-muted-foreground px-2">{t("chat.translating")}</span>
          )}
          {compact && (
            <Link to="/chat">
              <Button variant="ghost" size="icon" className="h-8 w-8">
                <Maximize2 className="h-4 w-4" />
              </Button>
            </Link>
          )}
          {onClose && (
            <Button variant="ghost" size="icon" onClick={onClose} className="h-8 w-8">
              <X className="h-4 w-4" />
            </Button>
          )}
        </div>
      </header>

      <div className="flex-1 overflow-y-auto px-5 py-5 space-y-5">
        {displayMessages.map((m, i) => (
          <div key={i} className={m.role === "user" ? "flex justify-end" : ""}>
            {m.role === "user" ? (
              <div className="max-w-[85%] rounded-2xl rounded-br-sm bg-primary text-primary-foreground px-4 py-2.5 text-sm">
                {m.content}
              </div>
            ) : (
              <div className="max-w-[92%]">
                <div className="text-sm leading-relaxed whitespace-pre-wrap text-foreground">
                  {m.content}
                </div>
                {m.sources && m.sources.length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    {m.sources.map((s) => (
                      <span
                        key={s}
                        className="text-[11px] px-2 py-0.5 rounded-full border border-border bg-muted/60 text-muted-foreground"
                      >
                        {s}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        ))}
        {chatSend.isPending && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> {t("chat.thinking")}
          </div>
        )}
      </div>

      {messages.length <= 1 && (
        <div className="px-5 pb-3 flex flex-wrap gap-1.5">
          {chatSuggestions(lang).map((s) => (
            <button
              key={s}
              onClick={() => send(s)}
              className="text-xs text-left px-3 py-1.5 rounded-full border border-border bg-muted/40 text-foreground/80 hover:bg-muted transition-colors"
            >
              {s}
            </button>
          ))}
        </div>
      )}

      <footer className="border-t border-border p-3">
        <div className="relative flex items-end gap-2 rounded-xl border border-input bg-background px-3 py-2 focus-within:ring-2 focus-within:ring-ring/40">
          <Textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send(input);
              }
            }}
            placeholder={t("chat.inputPlaceholder")}
            className="min-h-[36px] max-h-40 border-0 shadow-none focus-visible:ring-0 resize-none px-0 py-1 text-sm"
          />
          <Button
            size="icon"
            onClick={() => send(input)}
            disabled={chatSend.isPending}
            className="h-8 w-8 shrink-0 rounded-lg"
          >
            <Send className="h-4 w-4" />
          </Button>
        </div>
        <p className="text-[10px] text-muted-foreground text-center mt-1.5">
          {t("chat.disclaimer")}
        </p>
      </footer>
    </div>
  );
}

export function AiChatFab() {
  const { t } = useLanguage();
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="fixed bottom-6 right-6 z-40 h-14 w-14 rounded-full bg-primary text-primary-foreground shadow-lg shadow-primary/30 hover:scale-105 active:scale-95 transition-transform flex items-center justify-center group"
        aria-label={t("chat.fabLabel")}
      >
        <Sparkles className="h-6 w-6" />
        <span className="absolute right-full mr-3 whitespace-nowrap text-xs bg-foreground text-background px-2.5 py-1 rounded opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none">
          {t("chat.fabLabel")}
        </span>
      </button>

      {open && (
        <>
          <div
            className="fixed inset-0 z-40 bg-black/30 backdrop-blur-[2px]"
            onClick={() => setOpen(false)}
          />
          <div className="fixed bottom-24 right-6 z-50 w-[440px] h-[640px] max-h-[85vh] rounded-2xl border border-border shadow-2xl overflow-hidden">
            <AiChatPanel compact onClose={() => setOpen(false)} />
          </div>
        </>
      )}
    </>
  );
}
