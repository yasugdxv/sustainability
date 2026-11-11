// APIの接続先（全APIクライアントで共通）。
//   開発(npm run dev)     : http://127.0.0.1:8010（ローカルのAPIサーバー）
//   本番ビルド(npm run build): "" （同じサイトから配信される。FastAPIが /api と画面の両方を配信する）
//   上書き: ビルド時に VITE_API_BASE を指定すると、その値を使う（別ホストのAPIへ向けたいとき）
//
// 以前は各APIクライアントが "http://127.0.0.1:8010" を個別に固定していたため、サーバーへ置くと
// 利用者のPCではなくサーバー自身を見に行ってしまい、動かなかった。接続先は、ここだけで決める。
const fromEnv = import.meta.env.VITE_API_BASE as string | undefined;

export const API_BASE: string = fromEnv ? fromEnv.replace(/\/$/, "") : import.meta.env.PROD ? "" : "http://127.0.0.1:8010";
