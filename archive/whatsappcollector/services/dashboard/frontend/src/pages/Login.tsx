import { useState, FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { login, ApiError } from '../hooks/useApi'

export default function Login() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const navigate = useNavigate()

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setLoading(true)
    try {
      await login(username, password)
      navigate('/', { replace: true })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Login failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen bg-bg-base flex items-center justify-center p-4">
      <div className="w-full max-w-sm">
        {/* Logo */}
        <div className="text-center mb-8">
          <span className="font-mono text-3xl font-semibold text-white tracking-tight">WAC</span>
          <p className="text-text-muted text-sm mt-1">dashboard</p>
        </div>

        <form onSubmit={handleSubmit} className="card p-6 space-y-4">
          <div>
            <label className="block text-text-muted text-xs uppercase tracking-wider mb-1.5">
              Username
            </label>
            <input
              type="text"
              value={username}
              onChange={e => setUsername(e.target.value)}
              required
              autoFocus
              autoComplete="username"
              className="w-full bg-bg-elevated border border-border rounded-md px-3 py-2 text-sm text-white placeholder-text-muted focus:outline-none focus:border-white/40 font-mono"
              placeholder="admin"
            />
          </div>

          <div>
            <label className="block text-text-muted text-xs uppercase tracking-wider mb-1.5">
              Password
            </label>
            <input
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              required
              autoComplete="current-password"
              className="w-full bg-bg-elevated border border-border rounded-md px-3 py-2 text-sm text-white placeholder-text-muted focus:outline-none focus:border-white/40 font-mono"
              placeholder="••••••••"
            />
          </div>

          {error && (
            <p className="text-status-down text-xs font-mono bg-status-down/5 border border-status-down/20 rounded px-3 py-2">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={loading}
            className="w-full bg-white text-black font-medium text-sm py-2 rounded-md hover:bg-white/90 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {loading ? 'Signing in…' : 'Sign in'}
          </button>
        </form>

        <p className="text-center text-text-muted text-xs mt-4">
          WhatsApp Collector — Ops Dashboard
        </p>
      </div>
    </div>
  )
}
