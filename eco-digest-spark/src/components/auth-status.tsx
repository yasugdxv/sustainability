import { useEffect, useState } from "react";
import { LogIn, LogOut } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { getAccount, signIn, signOut, ssoEnabled, type AuthAccount } from "@/lib/auth";

// SSOが有効なときだけ表示する「サインイン／サインアウト」。承認者の判定は、サーバー側(APIがトークンを検証)で行う
export function AuthStatus() {
  const [account, setAccount] = useState<AuthAccount | null>(null);
  const [ready, setReady] = useState(false);
  const qc = useQueryClient();

  useEffect(() => {
    if (!ssoEnabled) return;
    let alive = true;
    getAccount()
      .then((a) => {
        if (!alive) return;
        setAccount(a);
        // ログインから戻った直後は、トークン無しで取得済みの承認者情報を、取り直す
        if (a) qc.invalidateQueries({ queryKey: ["reviewPrincipal"] });
      })
      .catch(() => undefined)
      .finally(() => alive && setReady(true));
    return () => {
      alive = false;
    };
  }, [qc]);

  if (!ssoEnabled || !ready) return null;

  const base =
    "flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-full border border-border text-muted-foreground hover:text-foreground hover:bg-muted/50 transition-colors";

  if (!account) {
    return (
      <button type="button" className={base} onClick={() => void signIn()} title="Microsoftアカウントでサインイン">
        <LogIn className="h-3.5 w-3.5" />
        <span className="hidden sm:inline">サインイン</span>
      </button>
    );
  }
  return (
    <button type="button" className={base} onClick={() => void signOut()} title={`${account.username} でサインイン中（クリックでサインアウト）`}>
      <LogOut className="h-3.5 w-3.5" />
      <span className="hidden sm:inline max-w-[10rem] truncate">{account.name}</span>
    </button>
  );
}
