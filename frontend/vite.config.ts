import path from "node:path";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

const INTER_FONT = "Inter";

function stripFigtreePlugin(): Plugin {
  const replaceFigtree = (source: string): string =>
    source.replace(/Figtree/gi, INTER_FONT);

  return {
    name: "foldesk-strip-figtree",
    enforce: "post",
    transform(code, id) {
      if (
        (id.endsWith(".css") || id.endsWith(".js") || id.endsWith(".mjs")) &&
        /figtree/i.test(code)
      ) {
        return { code: replaceFigtree(code), map: null };
      }
      return null;
    },
    generateBundle(_options, bundle) {
      for (const item of Object.values(bundle)) {
        if (item.type === "chunk" && /figtree/i.test(item.code)) {
          item.code = replaceFigtree(item.code);
        } else if (
          item.type === "asset" &&
          typeof item.source === "string" &&
          /figtree/i.test(item.source)
        ) {
          item.source = replaceFigtree(item.source);
        }
      }
    },
  };
}

export default defineConfig({
  plugins: [react(), stripFigtreePlugin()],
  build: {
    outDir: path.resolve(__dirname, "../app/static/inbox-v2"),
    emptyOutDir: true,
    cssCodeSplit: false,
    sourcemap: false,
    rollupOptions: {
      input: path.resolve(__dirname, "src/main.tsx"),
      output: {
        inlineDynamicImports: true,
        entryFileNames: "inbox-v2.js",
        assetFileNames: (assetInfo) => {
          if (assetInfo.name && assetInfo.name.endsWith(".css")) {
            return "inbox-v2.css";
          }
          return "assets/[name]-[hash][extname]";
        },
      },
    },
  },
});
