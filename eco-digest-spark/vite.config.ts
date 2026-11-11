// @lovable.dev/vite-tanstack-config already includes the following — do NOT add them manually
// or the app will break with duplicate plugins:
//   - TanStack devtools (dev-only, first), tanstackStart, viteReact, tailwindcss, tsConfigPaths,
//     nitro (build-only using cloudflare as a default target), VITE_* env injection, @ path alias,
//     React/TanStack dedupe, error logger plugins, and sandbox detection (port/host/strictPort).
// You can pass additional config via defineConfig({ vite: { ... }, etc... }) if needed.
import { defineConfig } from "@lovable.dev/vite-tanstack-config";

// WMD_STATIC_BUILD=1 のとき、画面を「静的ファイル（SPA）」としてビルドする。
// Azure App Service（Python）では、FastAPIが /api と画面の静的ファイルを同じサイトから配信するため、
// Nodeのサーバー（SSR）は使わない。指定しない通常の `npm run build` / `npm run dev` の動作は変わらない。
const staticBuild = process.env.WMD_STATIC_BUILD === "1";

export default defineConfig({
  tanstackStart: {
    // Redirect TanStack Start's bundled server entry to src/server.ts (our SSR error wrapper).
    // nitro/vite builds from this
    server: { entry: "server" },
    ...(staticBuild ? { spa: { enabled: true } } : {}),
  },
  // 静的ビルドではNitroを使わず、標準の dist/client + dist/server へ出力する
  // （SPAの初期HTMLを事前生成する処理が dist/server を使うため）
  nitro: staticBuild ? (false as const) : { preset: "node-server" },
});
