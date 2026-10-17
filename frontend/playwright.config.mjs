import { existsSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from '@playwright/test'

const frontendDirectory = dirname(fileURLToPath(import.meta.url))
const repositoryDirectory = resolve(frontendDirectory, '..')
const pythonPath = resolve(
  repositoryDirectory,
  '.venv-dev',
  process.platform === 'win32' ? 'Scripts' : 'bin',
  process.platform === 'win32' ? 'python.exe' : 'python',
)

if (!existsSync(pythonPath)) {
  throw new Error('The JARVIS E2E backend requires the repository .venv-dev Python environment.')
}

const backendUrl = 'http://127.0.0.1:8173'
const frontendUrl = 'http://127.0.0.1:5173'

export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.e2e.mjs',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: 'list',
  outputDir: '.playwright/test-results',
  preserveOutput: 'never',
  use: {
    baseURL: frontendUrl,
    browserName: 'chromium',
    serviceWorkers: 'block',
    trace: 'off',
    screenshot: 'off',
    video: 'off',
  },
  webServer: [
    {
      command: `"${pythonPath}" -m uvicorn jarvis.main:app --host 127.0.0.1 --port 8173 --no-access-log`,
      cwd: repositoryDirectory,
      env: {
        JARVIS_ENVIRONMENT: 'test',
        JARVIS_LOG_LEVEL: 'WARNING',
        JARVIS_PROVIDER: 'mock',
        JARVIS_PERSISTENCE_ENABLED: 'false',
        JARVIS_MEMORY_RETRIEVAL_ENABLED: 'false',
        // Synthetic local fixture identity; never use a real credential in browser tests.
        JARVIS_LOCAL_AUTH_TOKEN: 'jarvis-e2e-synthetic-token-never-a-secret-0001',
        JARVIS_OWNER_ID: '00000000-0000-4000-8000-000000000001',
        JARVIS_PERSISTENCE_DATABASE_URL: '',
        JARVIS_OPENAI_API_KEY: '',
      },
      url: `${backendUrl}/ready`,
      timeout: 60_000,
      reuseExistingServer: false,
      stdout: 'ignore',
      stderr: 'pipe',
    },
    {
      command: 'npm run dev -- --host 127.0.0.1 --port 5173 --strictPort',
      cwd: frontendDirectory,
      env: {
        VITE_API_BASE_URL: '',
        VITE_API_PROXY_TARGET: backendUrl,
      },
      url: frontendUrl,
      timeout: 60_000,
      reuseExistingServer: false,
      stdout: 'ignore',
      stderr: 'pipe',
    },
  ],
})
