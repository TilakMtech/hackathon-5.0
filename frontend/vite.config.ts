import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In dev, /api is proxied to the FastAPI backend. In production nginx does the same.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': process.env.VITE_API_PROXY ?? 'http://localhost:8000' } },
})
