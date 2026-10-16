import { fileURLToPath } from 'node:url';
import { defineConfig, type UserConfig } from 'vite';

const root = fileURLToPath(new URL('.', import.meta.url));

interface LibTarget {
  entry: string;
  fileName: string;
  format: 'es' | 'iife';
  outDir: string;
  publicDir?: string;
  emptyOutDir?: boolean;
}

const targets: Record<string, LibTarget> = {
  sw: { entry: 'src/background/service-worker.ts', fileName: 'service-worker.js', format: 'es', outDir: 'dist', publicDir: 'public', emptyOutDir: true },
  content: { entry: 'src/content/main.ts', fileName: 'content.js', format: 'iife', outDir: 'dist' },
  page: { entry: 'src/page/global.ts', fileName: 'page_script.js', format: 'iife', outDir: '../babas/drivers' },
};

export default defineConfig(({ mode }): UserConfig => {
  if (mode === 'sidepanel') {
    // App build of sidepanel.html and micpermission.html. It must run after "sw", which empties dist first.
    return {
      root,
      base: './',
      publicDir: false,
      logLevel: 'warn',
      build: {
        outDir: `${root}dist`,
        emptyOutDir: false,
        target: 'chrome116',
        minify: false,
        sourcemap: false,
        copyPublicDir: false,
        modulePreload: { polyfill: false },
        rollupOptions: {
          input: { sidepanel: `${root}sidepanel.html`, micpermission: `${root}micpermission.html` },
          output: { entryFileNames: 'assets/[name].js', chunkFileNames: 'assets/[name].js', assetFileNames: 'assets/[name][extname]' },
        },
      },
    };
  }
  const target = targets[mode];
  if (!target) throw new Error(`Unknown build mode "${mode}". Use one of: ${[...Object.keys(targets), 'sidepanel'].join(', ')}`);
  return {
    root,
    publicDir: target.publicDir ? `${root}${target.publicDir}` : false,
    logLevel: 'warn',
    build: {
      outDir: `${root}${target.outDir}`,
      emptyOutDir: target.emptyOutDir ?? false,
      target: 'chrome116',
      minify: false,
      sourcemap: false,
      copyPublicDir: !!target.publicDir,
      lib: {
        entry: `${root}${target.entry}`,
        formats: [target.format],
        name: 'BabasBundle',
        fileName: () => target.fileName,
      },
    },
  };
});
