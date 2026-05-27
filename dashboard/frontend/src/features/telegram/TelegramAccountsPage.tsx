import { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../../lib/api';
import { Button } from '../../components/ui/Button';
import { DataTable } from '../../components/ui/DataTable';
import { StatusBadge } from '../../components/ui/StatusBadge';
import { LoadingSpinner } from '../../components/ui/LoadingSpinner';
import { ErrorState } from '../../components/ui/ErrorState';

interface TelegramAccount {
  name: string;
  phone: string;
  phone_full: string;
  status: 'active' | 'disabled' | 'expired' | 'banned';
  owner_bot: string | null;
  created_at: string | null;
  last_connected_at: string | null;
  last_error: string | null;
}

type OnboardStep = 'idle' | 'phone' | 'code' | '2fa' | 'success' | 'error';

export function TelegramAccountsPage() {
  const queryClient = useQueryClient();
  const [showAddModal, setShowAddModal] = useState(false);

  const { data: accounts, isLoading, error } = useQuery<TelegramAccount[]>({
    queryKey: ['telegram-accounts'],
    queryFn: () => api.get('/api/telegram/accounts').then(r => r.data),
  });

  const deleteMutation = useMutation({
    mutationFn: (name: string) => api.delete(`/api/telegram/accounts/${name}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['telegram-accounts'] }),
  });

  const disableMutation = useMutation({
    mutationFn: (name: string) => api.post(`/api/telegram/accounts/${name}/disable`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['telegram-accounts'] }),
  });

  const enableMutation = useMutation({
    mutationFn: (name: string) => api.post(`/api/telegram/accounts/${name}/enable`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['telegram-accounts'] }),
  });

  if (isLoading) return <LoadingSpinner />;
  if (error) return <ErrorState message="Failed to load accounts" />;

  const columns = [
    { header: 'Name', accessor: 'name' as const },
    { header: 'Phone', accessor: 'phone' as const },
    { 
      header: 'Status', 
      accessor: 'status' as const,
      cell: (row: TelegramAccount) => (
        <StatusBadge status={row.status === 'active' ? 'success' : row.status === 'disabled' ? 'warning' : 'error'}>
          {row.status}
        </StatusBadge>
      )
    },
    { header: 'Source', accessor: 'owner_bot' as const },
    { 
      header: 'Last Connected', 
      accessor: 'last_connected_at' as const,
      cell: (row: TelegramAccount) => row.last_connected_at 
        ? new Date(row.last_connected_at).toLocaleString() 
        : '-'
    },
    {
      header: 'Actions',
      accessor: 'name' as const,
      cell: (row: TelegramAccount) => (
        <div className="flex gap-2">
          {row.status === 'active' ? (
            <Button 
              size="sm" 
              variant="secondary"
              onClick={() => disableMutation.mutate(row.name)}
              disabled={disableMutation.isPending}
            >
              Disable
            </Button>
          ) : row.status === 'disabled' ? (
            <Button 
              size="sm" 
              variant="primary"
              onClick={() => enableMutation.mutate(row.name)}
              disabled={enableMutation.isPending}
            >
              Enable
            </Button>
          ) : null}
          <Button 
            size="sm" 
            variant="danger"
            onClick={() => {
              if (confirm(`Delete account ${row.name}?`)) {
                deleteMutation.mutate(row.name);
              }
            }}
            disabled={deleteMutation.isPending}
          >
            Delete
          </Button>
        </div>
      )
    }
  ];

  return (
    <div className="p-6">
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-2xl font-bold">Telegram Accounts</h1>
        <Button onClick={() => setShowAddModal(true)}>Add Account</Button>
      </div>

      <div className="bg-white dark:bg-gray-800 rounded-lg shadow">
        <DataTable data={accounts || []} columns={columns} />
      </div>

      {showAddModal && (
        <AddAccountModal onClose={() => {
          setShowAddModal(false);
          queryClient.invalidateQueries({ queryKey: ['telegram-accounts'] });
        }} />
      )}
    </div>
  );
}

function AddAccountModal({ onClose }: { onClose: () => void }) {
  const [step, setStep] = useState<OnboardStep>('phone');
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [password, setPassword] = useState('');
  const [name, setName] = useState('');
  const [error, setError] = useState('');
  const [result, setResult] = useState<{ name: string; display_name: string } | null>(null);

  const requestCodeMutation = useMutation({
    mutationFn: () => api.post('/api/telegram/accounts/request-code', { phone, name: name || undefined }),
    onSuccess: () => {
      setStep('code');
      setError('');
    },
    onError: (err: any) => {
      setError(err.response?.data?.detail || 'Failed to send code');
    }
  });

  const verifyCodeMutation = useMutation({
    mutationFn: () => api.post('/api/telegram/accounts/verify-code', { 
      phone, 
      code, 
      password: password || undefined 
    }),
    onSuccess: (response) => {
      if (response.data.status === '2fa_required') {
        setStep('2fa');
        setError('');
      } else if (response.data.status === 'success') {
        setResult(response.data);
        setStep('success');
      }
    },
    onError: (err: any) => {
      setError(err.response?.data?.detail || 'Verification failed');
    }
  });

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
      <div className="bg-white dark:bg-gray-800 rounded-lg p-6 max-w-md w-full mx-4">
        <h2 className="text-xl font-bold mb-4">Add Telegram Account</h2>

        {error && (
          <div className="bg-red-100 dark:bg-red-900 text-red-700 dark:text-red-200 p-3 rounded mb-4">
            {error}
          </div>
        )}

        {step === 'phone' && (
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium mb-1">Phone Number</label>
              <input
                type="tel"
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
                placeholder="+6591234567"
                className="w-full border rounded px-3 py-2 dark:bg-gray-700 dark:border-gray-600"
              />
              <p className="text-xs text-gray-500 mt-1">Include country code</p>
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">Account Name (optional)</label>
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="my_account"
                className="w-full border rounded px-3 py-2 dark:bg-gray-700 dark:border-gray-600"
              />
            </div>
            <div className="flex gap-2 justify-end">
              <Button variant="secondary" onClick={onClose}>Cancel</Button>
              <Button 
                onClick={() => requestCodeMutation.mutate()}
                disabled={!phone || requestCodeMutation.isPending}
              >
                {requestCodeMutation.isPending ? 'Sending...' : 'Send Code'}
              </Button>
            </div>
          </div>
        )}

        {step === 'code' && (
          <div className="space-y-4">
            <p className="text-sm text-gray-600 dark:text-gray-400">
              Enter the verification code sent to {phone}
            </p>
            <div>
              <label className="block text-sm font-medium mb-1">Verification Code</label>
              <input
                type="text"
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
                placeholder="12345"
                maxLength={6}
                className="w-full border rounded px-3 py-2 dark:bg-gray-700 dark:border-gray-600 text-center text-2xl tracking-widest"
              />
            </div>
            <div className="flex gap-2 justify-end">
              <Button variant="secondary" onClick={() => setStep('phone')}>Back</Button>
              <Button 
                onClick={() => verifyCodeMutation.mutate()}
                disabled={code.length < 5 || verifyCodeMutation.isPending}
              >
                {verifyCodeMutation.isPending ? 'Verifying...' : 'Verify'}
              </Button>
            </div>
          </div>
        )}

        {step === '2fa' && (
          <div className="space-y-4">
            <p className="text-sm text-gray-600 dark:text-gray-400">
              Two-factor authentication is enabled. Enter your password.
            </p>
            <div>
              <label className="block text-sm font-medium mb-1">2FA Password</label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full border rounded px-3 py-2 dark:bg-gray-700 dark:border-gray-600"
              />
            </div>
            <div className="flex gap-2 justify-end">
              <Button variant="secondary" onClick={() => setStep('code')}>Back</Button>
              <Button 
                onClick={() => verifyCodeMutation.mutate()}
                disabled={!password || verifyCodeMutation.isPending}
              >
                {verifyCodeMutation.isPending ? 'Verifying...' : 'Submit'}
              </Button>
            </div>
          </div>
        )}

        {step === 'success' && result && (
          <div className="space-y-4">
            <div className="text-center">
              <div className="text-green-500 text-4xl mb-2">✓</div>
              <p className="font-medium">{result.display_name || result.name}</p>
              <p className="text-sm text-gray-500">Account connected successfully</p>
            </div>
            <Button className="w-full" onClick={onClose}>Done</Button>
          </div>
        )}
      </div>
    </div>
  );
}
