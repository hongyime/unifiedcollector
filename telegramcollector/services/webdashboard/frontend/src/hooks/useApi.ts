import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'

export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) { super(message); this.status = status }
}

export async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json', ...options?.headers }, ...options })
  if (!res.ok) {
    let message = `HTTP ${res.status}`
    try { const err = await res.json(); message = err.detail || err.error || message } catch {}
    throw new ApiError(message, res.status)
  }
  return res.json()
}

export function useApiQuery<T>(key: (string | number | boolean | null | undefined)[], path: string, options?: { enabled?: boolean; refetchInterval?: number }) {
  return useQuery<T, ApiError>({ queryKey: key, queryFn: () => apiFetch<T>(path), enabled: options?.enabled ?? true, refetchInterval: options?.refetchInterval })
}

export function useApiMutation<TData, TVariables>(mutationFn: (v: TVariables) => Promise<TData>, invalidateKeys?: string[][]) {
  const qc = useQueryClient()
  return useMutation<TData, ApiError, TVariables>({
    mutationFn,
    onSuccess: () => { invalidateKeys?.forEach(k => qc.invalidateQueries({ queryKey: k })) },
  })
}
