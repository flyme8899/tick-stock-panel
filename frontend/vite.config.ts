import { defineConfig, configDefaults } from 'vitest/config'
import react from '@vitejs/plugin-react'
import path from 'node:path'

const backendHost = process.env.BACKEND_HOST || '127.0.0.1'
const proxyHost = ['0.0.0.0', '::'].includes(backendHost) ? '127.0.0.1' : backendHost
const backendPort = process.env.BACKEND_PORT || '3018'
const backendTarget = `http://${proxyHost}:${backendPort}`

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    host: '0.0.0.0',   // dev.sh / dev.ps1 会用 CLI --host 覆盖
    port: 3011,
    proxy: {
      // dev 时 /api 转发到与启动脚本相同的 FastAPI 地址
      '/api': {
        target: backendTarget,
        // SSE 端点需要禁用缓冲
        configure: (proxy) => {
          proxy.on('proxyReq', (_proxyReq, req) => {
            if (req.url?.includes('/stream')) {
              _proxyReq.setHeader('Accept', 'text/event-stream')
              _proxyReq.setHeader('Cache-Control', 'no-cache')
              _proxyReq.setHeader('Connection', 'keep-alive')
            }
          })
        },
      },
      '/health': backendTarget,
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    rollupOptions: {
      output: {
        // 把重型图表库拆到独立 chunk, 避免打进主包 + 让页面按需加载。
        // 用函数形式按 node_modules 路径匹配, 比对象形式更可靠。
        manualChunks(id) {
          if (id.includes('node_modules')) {
            if (id.includes('echarts')) return 'echarts'
            if (id.includes('lightweight-charts')) return 'lightweight-charts'
          }
        },
      },
    },
  },
  test: {
    // 点名排除两个**不属于 vitest 体系**的文件, 理由:
    //
    // 它们用 `import { test } from 'node:test'` 写, 是 Node 内置测试运行器的
    // 用例。vitest 收集时会报 "No test suite found in file"（找不到 describe/it),
    // 直接算文件级失败。
    //
    // 而它们也**从来没有在任何地方跑过**: `node --test` 不认 `.ts` 扩展名
    // (ERR_UNKNOWN_FILE_EXTENSION, 需要 loader 或先编译), package.json 里也没有
    // 任何脚本调它们。所以这两个文件的真实状态是「零执行」—— 既没在 vitest 里跑,
    // 也没在 node 里跑。这是 #64 时期留下的历史遗留, 与本拍无关。
    //
    // 本拍的处理: 不删、不改写、不假装它们在跑 —— 只是不让它们把整个 vitest
    // 套件带红。要真正跑起来需要把它们改写成 vitest 语法(或给 node 配 tsx loader),
    // 那是另一件事, 记在 PR 描述的「另拍」里。
    //
    // 注意: 必须展开 configDefaults.exclude, 否则会丢掉 vitest 对 node_modules /
    // dist 的默认排除, 把依赖目录里的文件也扫进来。
    exclude: [
      ...configDefaults.exclude,
      'src/components/DimensionMembersDialog.test.ts',
      'src/lib/neighborPrefetch.test.ts',
    ],
  },
})
