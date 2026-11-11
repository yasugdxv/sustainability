// SSO（Microsoft Entra ID）ログイン。MSAL(認可コード+PKCE)でサインインし、
// 自社APIのスコープのアクセストークンを、APIへの呼び出しに付ける（Authorization: Bearer）。
// APIの検証側は、サーバーの entra_auth.py / identity.py。
//
// 設定（ビルド時の環境変数。秘密ではない値だけ。クライアントシークレットは、画面には絶対に置かない）:
//   VITE_ENTRA_TENANT_ID : 自社テナントID
//   VITE_ENTRA_CLIENT_ID : アプリ(クライアント)ID
//   VITE_ENTRA_SCOPE     : APIのスコープ（省略時は api://<クライアントID>/access_as_user）
// どちらかのIDが未設定ならSSOは無効で、従来どおり（トークンを付けずに呼ぶ）動く。
//
// MSALはブラウザ専用のため、使う場所で動的にimportする（静的ビルドの事前描画で、windowを触らないため）。
import type { AccountInfo, PublicClientApplication } from "@azure/msal-browser";

const clientId = (import.meta.env.VITE_ENTRA_CLIENT_ID as string | undefined)?.trim() || "";
const tenantId = (import.meta.env.VITE_ENTRA_TENANT_ID as string | undefined)?.trim() || "";
const scope =
  (import.meta.env.VITE_ENTRA_SCOPE as string | undefined)?.trim() || (clientId ? `api://${clientId}/access_as_user` : "");

export const ssoEnabled = Boolean(clientId && tenantId);

let msalPromise: Promise<PublicClientApplication> | null = null;

// MSALを1回だけ初期化する。リダイレクトでログインから戻ってきた場合は、ここで結果を取り込む。
// すべてのAPI呼び出しがこの完了を待つので、戻ってきた直後の最初の呼び出しにも、トークンが付く。
function getMsal(): Promise<PublicClientApplication> {
  if (!msalPromise) {
    msalPromise = (async () => {
      const { PublicClientApplication } = await import("@azure/msal-browser");
      const app = new PublicClientApplication({
        auth: {
          clientId,
          authority: `https://login.microsoftonline.com/${tenantId}`,
          redirectUri: window.location.origin,
          postLogoutRedirectUri: window.location.origin,
        },
        // タブを閉じたら消える（共用PCにトークンを残さない）
        cache: { cacheLocation: "sessionStorage" },
      });
      await app.initialize();
      const result = await app.handleRedirectPromise();
      const account = result?.account ?? app.getAllAccounts()[0] ?? null;
      if (account) app.setActiveAccount(account);
      return app;
    })();
    // 初期化に失敗したら、次の呼び出しでやり直せるようにする
    msalPromise.catch(() => {
      msalPromise = null;
    });
  }
  return msalPromise;
}

export interface AuthAccount {
  name: string;
  username: string;
}

function toAccount(a: AccountInfo | null): AuthAccount | null {
  return a ? { name: a.name || a.username, username: a.username } : null;
}

// いまサインインしている人（SSO無効・未サインインなら null）
export async function getAccount(): Promise<AuthAccount | null> {
  if (!ssoEnabled) return null;
  const app = await getMsal();
  return toAccount(app.getActiveAccount());
}

// APIに付けるアクセストークンを返す。未サインイン・取得できないときは null（その場合はトークンなしで呼ぶ）
export async function getAccessToken(): Promise<string | null> {
  if (!ssoEnabled) return null;
  try {
    const app = await getMsal();
    const account = app.getActiveAccount();
    if (!account) return null;
    const result = await app.acquireTokenSilent({ scopes: [scope], account });
    return result.accessToken || null;
  } catch {
    // 期限切れで、静かな更新ができない場合など。画面の「サインイン」から、やり直してもらう
    return null;
  }
}

export async function signIn(): Promise<void> {
  const app = await getMsal();
  await app.loginRedirect({ scopes: [scope] });
}

export async function signOut(): Promise<void> {
  const app = await getMsal();
  await app.logoutRedirect({ account: app.getActiveAccount() ?? undefined });
}

// fetch に、アクセストークン(あれば)を付けて呼ぶ。SSOが無効なら、ふつうの fetch と同じ
export async function authFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const token = await getAccessToken();
  if (!token) return fetch(input, init);
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${token}`);
  return fetch(input, { ...init, headers });
}
