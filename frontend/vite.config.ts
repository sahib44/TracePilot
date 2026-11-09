import react from '@vitejs/plugin-react';
import { loadEnv } from 'vite';
import { defineConfig } from 'vitest/config';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'TRACEPILOT_');
  const proxy = {
    '/api': {
      target: env.TRACEPILOT_INVESTIGATOR_URL || 'http://127.0.0.1:8002',
      rewrite: (path: string) => path.slice(4),
    },
  };
  return {
    plugins: [react()],
    server: { host: '127.0.0.1', proxy },
    preview: { host: '127.0.0.1', proxy },
    test: { environment: 'jsdom', setupFiles: ['./tests/setup.ts'], maxWorkers: 2 },
  };
});
