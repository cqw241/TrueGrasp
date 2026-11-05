import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
  build: {
    // MathLive 单独约 800 kB，只随学生辅导台按需加载。
    chunkSizeWarningLimit: 1200,
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        // 代理默认等第一段响应体才发出响应头，SSE 在第一条事件之前 EventSource 一直连不上。
        // 响应头在 proxyRes 之后同步复制，下一个 tick 立即发出。
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes, _req, res) => {
            if (
              proxyRes.headers['content-type']?.startsWith('text/event-stream')
            ) {
              process.nextTick(() => res.flushHeaders())
            }
          })
        },
      },
    },
  },
})
