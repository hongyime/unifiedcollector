import { useState } from 'react'
import { ChevronUp, ChevronDown } from 'lucide-react'

export interface Column<T> {
  key: keyof T | string
  header: string
  render?: (value: unknown, row: T) => React.ReactNode
  sortable?: boolean
  mono?: boolean
  truncate?: boolean
  width?: string
}

interface DataTableProps<T extends Record<string, unknown>> {
  columns: Column<T>[]
  data: T[]
  rowKey: keyof T | ((row: T) => string | number)
  emptyMessage?: string
  className?: string
  maxHeight?: string
}

export default function DataTable<T extends Record<string, unknown>>({ columns, data, rowKey, emptyMessage = 'No data', className = '', maxHeight }: DataTableProps<T>) {
  const [sortKey, setSortKey] = useState<string | null>(null)
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc')

  const handleSort = (key: string) => {
    if (sortKey === key) setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    else { setSortKey(key); setSortDir('asc') }
  }

  const sorted = sortKey
    ? [...data].sort((a, b) => {
        const av = a[sortKey], bv = b[sortKey]
        if (av == null) return 1; if (bv == null) return -1
        const cmp = String(av).localeCompare(String(bv), undefined, { numeric: true })
        return sortDir === 'asc' ? cmp : -cmp
      })
    : data

  const getKey = (row: T, idx: number) => typeof rowKey === 'function' ? rowKey(row) : String(row[rowKey] ?? idx)

  return (
    <div className={`overflow-auto ${className}`} style={maxHeight ? { maxHeight } : undefined}>
      <table className="w-full text-sm border-collapse">
        <thead className="sticky top-0 z-10">
          <tr className="bg-bg-elevated border-b border-border">
            {columns.map(col => (
              <th
                key={String(col.key)}
                className={['text-left px-3 py-2.5 text-text-muted text-xs font-medium uppercase tracking-wider whitespace-nowrap', col.sortable ? 'cursor-pointer hover:text-white select-none' : '', col.width ?? ''].filter(Boolean).join(' ')}
                onClick={col.sortable ? () => handleSort(String(col.key)) : undefined}
              >
                <span className="flex items-center gap-1">
                  {col.header}
                  {col.sortable && sortKey === String(col.key) && (sortDir === 'asc' ? <ChevronUp size={12} /> : <ChevronDown size={12} />)}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.length === 0 ? (
            <tr><td colSpan={columns.length} className="px-3 py-8 text-center text-text-muted text-sm">{emptyMessage}</td></tr>
          ) : sorted.map((row, idx) => (
            <tr key={getKey(row, idx)} className="border-b border-border/50 hover:bg-accent-5 transition-colors">
              {columns.map(col => {
                const value = row[col.key as keyof T]
                const content = col.render ? col.render(value, row) : value == null ? <span className="text-text-muted">—</span> : String(value)
                return (
                  <td key={String(col.key)} className={['px-3 py-2.5 text-text-primary', col.mono ? 'font-mono text-xs' : '', col.truncate ? 'max-w-[200px] truncate' : ''].filter(Boolean).join(' ')}>
                    {content}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
