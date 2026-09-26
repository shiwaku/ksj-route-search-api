import tailwindcss from '@tailwindcss/vite';
import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [
		tailwindcss(),
		sveltekit({
			compilerOptions: {
				// Runes 記法を強制
				runes: ({ filename }) => (filename.split(/[/\\]/).includes('node_modules') ? undefined : true)
			},
			// 公開版は Workers Static Assets に置く静的ファイル。SPA なので未知のパスは index.html（+page.ts で ssr = false）
			adapter: adapter({ fallback: 'index.html' })
		})
	],
	// API は本番もローカルも同一オリジンの /api（deploy-cloudflare.md 6 章）。ローカルは Vite が /api を剥がして 8000 に渡す
	server: {
		proxy: { '/api': { target: 'http://localhost:8000', rewrite: (p) => p.replace(/^\/api/, '') } }
	}
});
