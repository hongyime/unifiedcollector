import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'

export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

// ── Token storage (sessionStorage — cleared on tab close, no XSS via localStorage) ──
const TOKEN_KEY = 'wac_dash_token'

export function getToken(): string | null {
  return sessionStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string): void {
  sessionStorage.setItem(TOKEN_KEY, token)
}

export function clearToken(): void {
  sessionStorage.removeItem(TOKEN_KEY)
}

export function isAuthenticated(): boolean {
  return !!getToken()
}

// ── Core fetch — injects bearer token, redirects on 401 ──────────────────────
export async function apiFetch<T>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  const token = getToken()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options?.headers as Record<string, string> ?? {}),
  }
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }

  const res = await fetch(path, { ...options, headers })

  if (res.status === 401) {
    clearToken()
    // Redirect to login without full reload — let React Router handle it
    window.location.href = '/login'
    throw new ApiError('Session expired', 401)
  }

  if (!res.ok) {
    let message = `HTTP ${res.status}`
    try {
      const err = await res.json()
      message = err.detail || err.error || message
    } catch {
      // ignore
    }
    throw new ApiError(message, res.status)
  }
  return res.json()
}

// ── Login helper ──────────────────────────────────────────────────────────────
export async function login(username: string, password: string): Promise<{ token: string; role: string }> {
  const res = await fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new ApiError(err.detail || 'Invalid credentials', res.status)
  }
  const data = await res.json()
  setToken(data.token)
  return data
}

export async function logout(): Promise<void> {
  const token = getToken()
  if (token) {
    await fetch('/api/auth/logout', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
    }).catch(() => {})
  }
  clearToken()
}

// ── WebSocket URL with auth token ─────────────────────────────────────────────
export function wsHealthUrl(): string {
  const token = getToken()
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const host = window.location.host
  const params = token ? `?token=${encodeURIComponent(token)}` : ''
  return `${proto}//${host}/ws/health${params}`
}

// ── TanStack Query wrappers ───────────────────────────────────────────────────
export function useApiQuery<T>(
  key: (string | number | boolean | null | undefined)[],
  path: string,
  options?: { enabled?: boolean; refetchInterval?: number },
) {
  return useQuery<T, ApiError>({
    queryKey: key,
    queryFn: () => apiFetch<T>(path),
    enabled: options?.enabled ?? true,
    refetchInterval: options?.refetchInterval,
    retry: (failureCount, error) => {
      // Don't retry auth failures
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) return false
      return failureCount < 2
    },
  })
}

export function useApiMutation<TData, TVariables>(
  mutationFn: (variables: TVariables) => Promise<TData>,
  invalidateKeys?: string[][],
) {
  const queryClient = useQueryClient()
  return useMutation<TData, ApiError, TVariables>({
    mutationFn,
    onSuccess: () => {
      if (invalidateKeys) {
        invalidateKeys.forEach((key) => queryClient.invalidateQueries({ queryKey: key }))
      }
    },
  })
}
