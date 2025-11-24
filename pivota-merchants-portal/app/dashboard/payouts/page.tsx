'use client';

import { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';
import { 
  ArrowLeft, 
  DollarSign, 
  Upload, 
  Download, 
  Plus,
  CheckCircle,
  Clock,
  FileText,
  ExternalLink,
  Filter,
  Search,
  CreditCard,
  AlertCircle,
  Building,
  MapPin,
  Copy
} from 'lucide-react';
import { apiClient } from '@/lib/api-client';

interface Payout {
  id: number;
  agent_id: string;
  amount: number;
  currency: string;
  status: 'pending' | 'uploaded' | 'paid';
  payout_reference?: string;
  file_url?: string;
  method?: string;
  provider?: string;
  period_start: string;
  period_end: string;
  uploaded_at?: string;
  confirmed_at?: string;
  created_at: string;
}

interface PayoutSummary {
  total_count: number;
  total_amount: number;
  unique_agents: number;
}

interface PendingCommission {
  agent_id: string;
  transaction_count: number;
  total_commission: number;
  currency: string;
  earliest_transaction?: string;
  latest_transaction?: string;
}

interface PendingCommissionSummary {
  total_amount: number;
  total_transactions: number;
  unique_agents: number;
  period_days: number;
}

export default function PayoutsPage() {
  const router = useRouter();
  const [payouts, setPayouts] = useState<Payout[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedStatus, setSelectedStatus] = useState<string>('all');
  const [searchTerm, setSearchTerm] = useState('');
  const [summary, setSummary] = useState<PayoutSummary | null>(null);
  const [showUploadModal, setShowUploadModal] = useState(false);
  const [selectedPayout, setSelectedPayout] = useState<Payout | null>(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showBankDetailsModal, setShowBankDetailsModal] = useState(false);
  const [agentBankDetails, setAgentBankDetails] = useState<any>(null);
  const [loadingBankDetails, setLoadingBankDetails] = useState(false);
  const [showProofModal, setShowProofModal] = useState(false);
  const [proofUrl, setProofUrl] = useState<string>('');
  
  // Pending commissions (not yet converted to payouts)
  const [pendingCommissions, setPendingCommissions] = useState<PendingCommission[]>([]);
  const [pendingCommissionSummary, setPendingCommissionSummary] = useState<PendingCommissionSummary | null>(null);
  const [generatingPayouts, setGeneratingPayouts] = useState(false);

  useEffect(() => {
    const token = localStorage.getItem('merchant_token');
    if (!token) {
      router.push('/login');
      return;
    }
    loadPayouts();
    loadPendingCommissions();
  }, [selectedStatus]);

  const loadPayouts = async () => {
    try {
      setLoading(true);
      const merchantId = localStorage.getItem('merchant_id');
      if (!merchantId) return;

      const params = new URLSearchParams();
      if (selectedStatus !== 'all') params.append('status', selectedStatus);
      
      const response = await apiClient.get(`/merchants/${merchantId}/payouts?${params}`);
      setPayouts(response.data.items || []);
      setSummary(response.data.summary || null);
    } catch (error) {
      console.error('Failed to load payouts:', error);
    } finally {
      setLoading(false);
    }
  };

  const loadPendingCommissions = async () => {
    try {
      const merchantId = localStorage.getItem('merchant_id');
      if (!merchantId) return;

      const response = await apiClient.get(`/merchants/${merchantId}/payouts/pending-commissions?days=180`);
      
      // Backend returns: { status, summary: {...}, agents: [...] }
      if (response.data.status === 'success') {
        setPendingCommissions(response.data.agents || []);
        setPendingCommissionSummary(response.data.summary || null);
      } else {
        setPendingCommissions([]);
        setPendingCommissionSummary(null);
      }
    } catch (error) {
      console.error('Failed to load pending commissions:', error);
    }
  };

  const generatePayoutsFromCommissions = async () => {
    if (!confirm(
      `This will create payout records for all agents with unpaid commissions from the last 180 days. Continue?`
    )) {
      return;
    }

    try {
      setGeneratingPayouts(true);
      const merchantId = localStorage.getItem('merchant_id');
      if (!merchantId) return;

      const response = await apiClient.post(
        `/merchants/${merchantId}/payouts/generate-from-commissions?days=180`
      );

      if (response.data.status === 'success') {
        const count = response.data.payouts_created;
        
        // Show success message with instructions
        alert(
          `✅ Successfully created ${count} payout(s)!\n\n` +
          `Next steps:\n` +
          `1. Review the pending payouts below\n` +
          `2. Download payment details (includes bank info)\n` +
          `3. Make payments to agents\n` +
          `4. Upload payment proof for each payout`
        );
        
        // Reload both payouts and pending commissions
        await loadPayouts();
        await loadPendingCommissions();
        
        // Scroll to payouts table
        window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
      }
    } catch (error: any) {
      console.error('Failed to generate payouts:', error);
      alert(error.response?.data?.detail || 'Failed to generate payouts');
    } finally {
      setGeneratingPayouts(false);
    }
  };

  const viewAgentBankDetails = async (agentId: string) => {
    try {
      setLoadingBankDetails(true);
      const merchantId = localStorage.getItem('merchant_id');
      if (!merchantId) return;

      const response = await apiClient.get(`/merchants/${merchantId}/agents/${agentId}/bank-details`);
      
      setAgentBankDetails(response.data);
      setShowBankDetailsModal(true);
    } catch (error: any) {
      console.error('Failed to load bank details:', error);
      alert(error.response?.data?.detail || 'Failed to load bank details');
    } finally {
      setLoadingBankDetails(false);
    }
  };

  const exportPaymentDetails = async () => {
    try {
      const merchantId = localStorage.getItem('merchant_id');
      if (!merchantId) return;

      // Get pending payouts
      const response = await apiClient.get(`/merchants/${merchantId}/payouts?status=pending`);
      const payouts = response.data.items || [];
      
      if (payouts.length === 0) {
        alert('No pending payouts to export');
        return;
      }

      // Fetch bank details for each agent
      const payoutsWithBank = await Promise.all(
        payouts.map(async (p: any) => {
          try {
            const bankResp = await apiClient.get(`/merchants/${merchantId}/agents/${p.agent_id}/bank-details`);
            const bankData = bankResp.data;
            
            return {
              ...p,
              bank_status: bankData.status,
              bank_details: bankData.bank_details,
              agent_name: bankData.agent?.name,
              agent_email: bankData.agent?.email
            };
          } catch (error) {
            console.error(`Failed to get bank for ${p.agent_id}:`, error);
            return {
              ...p,
              bank_status: 'error',
              bank_details: null,
              agent_name: null,
              agent_email: p.agent_id
            };
          }
        })
      );

      // Create CSV with payment instructions
      const headers = [
        'Payout ID',
        'Agent Name',
        'Agent Email',
        'Agent ID',
        'Amount',
        'Currency',
        'Period Start',
        'Period End',
        'Account Holder',
        'IBAN/Account',
        'SWIFT/BIC/Routing',
        'Bank Name',
        'Bank Country',
        'Payment Method',
        'Instructions',
        'Created Date'
      ];
      
      const rows = payoutsWithBank.map((p: any) => {
        const bank = p.bank_details;
        const status = p.bank_status;
        
        let accountInfo = '';
        let instructions = '';
        
        if (status === 'success' && bank) {
          // Use full IBAN or account number when available
          if (bank.iban) {
            accountInfo = bank.iban; // Full IBAN
          } else if (bank.account_number) {
            accountInfo = bank.account_number; // Full account number
          } else {
            accountInfo = bank.iban_preview || `****${bank.account_number_last4 || ''}`;
          }
          instructions = 'Use bank details provided. Contact agent if full details needed.';
        } else if (status === 'not_shared') {
          accountInfo = 'Not shared';
          instructions = `Contact ${p.agent_email} to obtain bank account details`;
        } else if (status === 'not_configured') {
          accountInfo = 'Not set up';
          instructions = `Agent needs to set up bank account. Contact ${p.agent_email}`;
        } else {
          accountInfo = 'N/A';
          instructions = `Contact ${p.agent_email} for payment details`;
        }
        
        // Get SWIFT/BIC or Routing number
        let routingInfo = '';
        if (status === 'success' && bank) {
          if (bank.swift_bic) {
            routingInfo = bank.swift_bic;
          } else if (bank.routing_number) {
            routingInfo = bank.routing_number;
          } else {
            routingInfo = 'N/A';
          }
        } else {
          routingInfo = 'N/A';
        }
        
        return [
          p.id,
          p.agent_name || 'N/A',
          p.agent_email || p.agent_id,
          p.agent_id,
          p.amount,
          p.currency,
          new Date(p.period_start).toLocaleDateString(),
          new Date(p.period_end).toLocaleDateString(),
          bank?.account_holder_name || 'N/A',
          accountInfo,
          routingInfo,
          bank?.bank_name || 'N/A',
          bank?.bank_country || 'N/A',
          bank?.method?.replace('_', ' ') || 'N/A',
          instructions,
          new Date(p.created_at).toLocaleDateString()
        ];
      });
      
      const csv = [
        headers.join(','),
        ...rows.map(r => r.map(cell => `"${String(cell).replace(/"/g, '""')}"`).join(','))
      ].join('\n');
      
      // Download
      const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `payout_instructions_${new Date().toISOString().split('T')[0]}.csv`;
      a.click();
      URL.revokeObjectURL(url);
      
      alert(`✅ Payment instructions exported!\n\nDownloaded file with ${payouts.length} payout(s) including bank details.`);
    } catch (error) {
      console.error('Failed to export payment details:', error);
      alert('Failed to export payment details');
    }
  };

  const exportPayouts = async () => {
    try {
      const merchantId = localStorage.getItem('merchant_id');
      if (!merchantId) return;

      const params = new URLSearchParams();
      if (selectedStatus !== 'all') params.append('status', selectedStatus);
      
      const response = await apiClient.get(`/merchants/${merchantId}/payouts/export/csv?${params}`);
      
      // Convert to CSV
      const data = response.data.data;
      const headers = Object.keys(data[0] || {});
      const csv = [
        headers.join(','),
        ...data.map((row: any) => headers.map(h => row[h]).join(','))
      ].join('\n');
      
      // Download
      const blob = new Blob([csv], { type: 'text/csv' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = response.data.filename || 'payouts.csv';
      a.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      console.error('Failed to export payouts:', error);
      alert('Failed to export payouts');
    }
  };

  const handleUploadProof = async (payout: Payout, data: any) => {
    try {
      const merchantId = localStorage.getItem('merchant_id');
      if (!merchantId) return;

      await apiClient.post(`/merchants/${merchantId}/payouts/${payout.id}/upload`, data);
      
      alert('Payment proof uploaded successfully!');
      setShowUploadModal(false);
      setSelectedPayout(null);
      loadPayouts();
    } catch (error: any) {
      console.error('Failed to upload proof:', error);
      alert(error.response?.data?.detail || 'Failed to upload payment proof');
    }
  };

  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'pending':
        return <Clock className="w-4 h-4 text-yellow-600" />;
      case 'uploaded':
        return <FileText className="w-4 h-4 text-blue-600" />;
      case 'paid':
        return <CheckCircle className="w-4 h-4 text-green-600" />;
      default:
        return null;
    }
  };

  const getStatusBadge = (status: string) => {
    const colors = {
      pending: 'bg-yellow-50 text-yellow-700 border-yellow-200',
      uploaded: 'bg-blue-50 text-blue-700 border-blue-200',
      paid: 'bg-green-50 text-green-700 border-green-200'
    };
    
    return (
      <span className={`inline-flex items-center gap-1 px-2 py-1 text-xs font-medium rounded-full border ${colors[status as keyof typeof colors]}`}>
        {getStatusIcon(status)}
        <span className="capitalize">{status}</span>
      </span>
    );
  };

  const filteredPayouts = payouts.filter(payout => 
    searchTerm === '' || 
    payout.agent_id.toLowerCase().includes(searchTerm.toLowerCase()) ||
    payout.payout_reference?.toLowerCase().includes(searchTerm.toLowerCase())
  );

  return (
    <div className="min-h-screen bg-gradient-to-br from-purple-50 via-white to-blue-50">
      {/* Header */}
      <header className="bg-white shadow-sm border-b sticky top-0 z-10">
        <div className="px-6 py-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center space-x-4">
              <Link href="/dashboard" className="text-gray-600 hover:text-gray-900">
                <ArrowLeft className="w-5 h-5" />
              </Link>
              <div>
                <h1 className="text-2xl font-bold text-gray-900">Agent Payouts</h1>
                <p className="text-sm text-gray-600 mt-1">Manage commission payouts to agents</p>
              </div>
            </div>
            <div className="flex items-center gap-3">
              <button
                onClick={() => setShowCreateModal(true)}
                className="flex items-center gap-2 px-4 py-2 bg-purple-600 text-white rounded-lg hover:bg-purple-700 transition-colors"
              >
                <Plus className="w-5 h-5" />
                <span>Create Payout</span>
              </button>
              <button
                onClick={exportPaymentDetails}
                className="flex items-center gap-2 px-4 py-2 border border-gray-300 rounded-lg hover:bg-gray-50 transition-colors"
              >
                <Download className="w-5 h-5" />
                <span>Export Payment Details</span>
              </button>
            </div>
          </div>
        </div>
      </header>

      <main className="px-6 py-8">
        {/* Pending Commissions Section - Always show if there are unpaid commissions */}
        {pendingCommissionSummary && pendingCommissionSummary.total_amount > 0 && (
          <div className="mb-8">
            {/* Info Banner */}
            <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-4 mb-4">
              <div className="flex items-start gap-3">
                <Clock className="w-5 h-5 text-yellow-600 mt-0.5" />
                <div className="flex-1">
                  <p className="text-sm font-medium text-yellow-900">
                    Unpaid Commissions Found
                  </p>
                  <p className="text-sm text-yellow-700 mt-1">
                    You have <strong>${pendingCommissionSummary.total_amount.toFixed(2)}</strong> in commissions 
                    owed to <strong>{pendingCommissionSummary.unique_agents} agents</strong> from the last 30 days. 
                    These commissions haven't been converted to payout records yet.
                  </p>
                </div>
              </div>
            </div>

            {/* Pending Commissions Card */}
            <div className="bg-white rounded-lg shadow-sm border border-gray-200 overflow-hidden">
              <div className="p-6 border-b border-gray-200">
                <div className="flex items-center justify-between">
                  <div>
                    <h3 className="text-lg font-semibold text-gray-900">Unpaid Commissions (Last 30 Days)</h3>
                    <p className="text-sm text-gray-600 mt-1">
                      Commission earned by agents but not yet paid
                    </p>
                  </div>
                  <button
                    onClick={generatePayoutsFromCommissions}
                    disabled={generatingPayouts}
                    className="flex items-center gap-2 px-4 py-2 bg-purple-600 text-white rounded-lg hover:bg-purple-700 transition-colors disabled:opacity-50"
                  >
                    {generatingPayouts ? (
                      <>
                        <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-white"></div>
                        <span>Generating...</span>
                      </>
                    ) : (
                      <>
                        <Plus className="w-5 h-5" />
                        <span>Generate Payouts</span>
                      </>
                    )}
                  </button>
                </div>
              </div>

              {/* Commissions Table */}
              <div className="overflow-x-auto">
                <table className="w-full">
                  <thead className="bg-gray-50">
                    <tr>
                      <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                        Agent ID
                      </th>
                      <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                        Transactions
                      </th>
                      <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                        Total Commission
                      </th>
                      <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                        Period
                      </th>
                    </tr>
                  </thead>
                  <tbody className="bg-white divide-y divide-gray-200">
                    {pendingCommissions.map((commission) => (
                      <tr key={commission.agent_id} className="hover:bg-gray-50">
                        <td className="px-6 py-4 whitespace-nowrap">
                          <div className="text-sm font-medium text-gray-900">
                            {commission.agent_id}
                          </div>
                        </td>
                        <td className="px-6 py-4 whitespace-nowrap">
                          <div className="text-sm text-gray-600">
                            {commission.transaction_count} orders
                          </div>
                        </td>
                        <td className="px-6 py-4 whitespace-nowrap">
                          <div className="text-sm font-semibold text-gray-900">
                            ${Number(commission.total_commission).toFixed(2)} {commission.currency}
                          </div>
                        </td>
                        <td className="px-6 py-4 whitespace-nowrap">
                          <div className="text-sm text-gray-600">
                            {commission.earliest_transaction 
                              ? `${new Date(commission.earliest_transaction).toLocaleDateString()} - ${new Date(commission.latest_transaction!).toLocaleDateString()}`
                              : 'N/A'
                            }
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {/* Summary Footer */}
              <div className="px-6 py-4 bg-gray-50 border-t border-gray-200">
                <div className="flex items-center justify-between text-sm">
                  <span className="text-gray-600">
                    Total: {pendingCommissionSummary.total_transactions} transactions from {pendingCommissionSummary.unique_agents} agents
                  </span>
                  <span className="font-semibold text-gray-900">
                    Total Amount: ${pendingCommissionSummary.total_amount.toFixed(2)}
                  </span>
                </div>
              </div>
            </div>

            {/* How it works */}
            <div className="mt-4 p-4 bg-blue-50 border border-blue-200 rounded-lg">
              <p className="text-sm font-medium text-blue-900 mb-2">How Payouts Work:</p>
              <ol className="text-sm text-blue-700 space-y-1 ml-4 list-decimal">
                <li><strong>Generate Payouts</strong>: Click the button above to create payout records from unpaid commissions</li>
                <li><strong>Pay Agents</strong>: Make the actual payment to agents (bank transfer, Stripe Connect, etc.)</li>
                <li><strong>Upload Proof</strong>: Upload payment confirmation and reference number</li>
                <li><strong>Confirm</strong>: Employee reviews and marks as paid - then agents can see the payment</li>
              </ol>
            </div>
          </div>
        )}

        {/* Summary Cards - Always show */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
            <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-6">
              <div className="flex items-center justify-between mb-2">
                <div className="p-2 bg-purple-50 rounded-lg">
                  <DollarSign className="w-6 h-6 text-purple-600" />
                </div>
              </div>
              <p className="text-sm text-gray-600 mb-1">Total Amount</p>
              <p className="text-2xl font-bold text-gray-900">
                ${summary?.total_amount?.toFixed(2) || '0.00'}
              </p>
            </div>

            <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-6">
              <div className="flex items-center justify-between mb-2">
                <div className="p-2 bg-blue-50 rounded-lg">
                  <FileText className="w-6 h-6 text-blue-600" />
                </div>
              </div>
              <p className="text-sm text-gray-600 mb-1">Total Payouts</p>
              <p className="text-2xl font-bold text-gray-900">
                {summary?.total_count || 0}
              </p>
            </div>

            <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-6">
              <div className="flex items-center justify-between mb-2">
                <div className="p-2 bg-green-50 rounded-lg">
                  <CheckCircle className="w-6 h-6 text-green-600" />
                </div>
              </div>
              <p className="text-sm text-gray-600 mb-1">Unique Agents</p>
              <p className="text-2xl font-bold text-gray-900">
                {summary?.unique_agents || 0}
              </p>
            </div>
          </div>

        {/* Filters */}
        <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-4 mb-6">
          <div className="flex flex-col md:flex-row gap-4">
            <div className="flex-1 relative">
              <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 text-gray-400 w-5 h-5" />
              <input
                type="text"
                placeholder="Search by agent ID or reference..."
                value={searchTerm}
                onChange={(e) => setSearchTerm(e.target.value)}
                className="w-full pl-10 pr-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-purple-500 focus:border-transparent"
              />
            </div>
            <div className="flex items-center gap-2">
              <Filter className="w-5 h-5 text-gray-500" />
              <select
                value={selectedStatus}
                onChange={(e) => setSelectedStatus(e.target.value)}
                className="px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-purple-500 focus:border-transparent"
              >
                <option value="all">All Status</option>
                <option value="pending">Pending</option>
                <option value="uploaded">Uploaded</option>
                <option value="paid">Paid</option>
              </select>
            </div>
          </div>
        </div>

        {/* Payouts Table */}
        <div className="bg-white rounded-lg shadow-sm overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="bg-gray-50 border-b border-gray-200">
                <tr>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                    Agent
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                    Amount
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                    Period
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                    Status
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                    Reference
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                    Actions
                  </th>
                </tr>
              </thead>
              <tbody className="bg-white divide-y divide-gray-200">
                {loading ? (
                  <tr>
                    <td colSpan={6} className="px-6 py-12 text-center text-gray-500">
                      <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-purple-600 mx-auto mb-2"></div>
                      Loading payouts...
                    </td>
                  </tr>
                ) : filteredPayouts.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="px-6 py-12 text-center text-gray-500">
                      No payouts found
                    </td>
                  </tr>
                ) : (
                  filteredPayouts.map((payout) => (
                    <tr key={payout.id} className="hover:bg-gray-50">
                      <td className="px-6 py-4 whitespace-nowrap">
                        <div className="text-sm font-medium text-gray-900">
                          {payout.agent_id}
                        </div>
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap">
                        <div className="text-sm font-semibold text-gray-900">
                          ${payout.amount.toFixed(2)} {payout.currency}
                        </div>
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap">
                        <div className="text-sm text-gray-600">
                          {new Date(payout.period_start).toLocaleDateString()} - {new Date(payout.period_end).toLocaleDateString()}
                        </div>
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap">
                        {getStatusBadge(payout.status)}
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap">
                        <div className="text-sm text-gray-600">
                          {payout.payout_reference || '-'}
                        </div>
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap">
                        <div className="flex items-center gap-2">
                          {payout.status === 'pending' && (
                            <>
                              <button
                                onClick={() => viewAgentBankDetails(payout.agent_id)}
                                disabled={loadingBankDetails}
                                className="text-green-600 hover:text-green-800 font-medium text-sm flex items-center gap-1"
                              >
                                <CreditCard className="w-4 h-4" />
                                View Bank
                              </button>
                              <button
                                onClick={() => {
                                  setSelectedPayout(payout);
                                  setShowUploadModal(true);
                                }}
                                className="text-purple-600 hover:text-purple-800 font-medium text-sm flex items-center gap-1"
                              >
                                <Upload className="w-4 h-4" />
                                Upload Proof
                              </button>
                            </>
                          )}
                          {payout.file_url && (
                            <button
                              onClick={() => {
                                setProofUrl(payout.file_url!);
                                setShowProofModal(true);
                              }}
                              className="text-blue-600 hover:text-blue-800 font-medium text-sm flex items-center gap-1"
                            >
                              <ExternalLink className="w-4 h-4" />
                              View Proof
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      </main>

      {/* Upload Modal */}
      {showUploadModal && selectedPayout && (
        <UploadProofModal
          payout={selectedPayout}
          onClose={() => {
            setShowUploadModal(false);
            setSelectedPayout(null);
          }}
          onUpload={handleUploadProof}
        />
      )}

      {/* Create Payout Modal */}
      {showCreateModal && (
        <CreatePayoutModal
          onClose={() => setShowCreateModal(false)}
          onSuccess={() => {
            setShowCreateModal(false);
            loadPayouts();
          }}
        />
      )}

      {/* Bank Details Modal */}
      {showBankDetailsModal && agentBankDetails && (
        <AgentBankDetailsModal
          bankData={agentBankDetails}
          onClose={() => {
            setShowBankDetailsModal(false);
            setAgentBankDetails(null);
          }}
        />
      )}

      {/* Proof Modal */}
      {showProofModal && proofUrl && (
        <ProofModal
          proofUrl={proofUrl}
          onClose={() => {
            setShowProofModal(false);
            setProofUrl('');
          }}
        />
      )}
    </div>
  );
}

// Upload Proof Modal Component
function UploadProofModal({ payout, onClose, onUpload }: any) {
  const [formData, setFormData] = useState({
    reference: '',
    file_url: '',
    method: 'wire',
    provider: '',
    external_id: ''
  });
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      setFile(e.target.files[0]);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!formData.reference) {
      alert('Payment reference is required');
      return;
    }

    if (file) {
      setUploading(true);
      // Convert file to base64 for now (later can implement proper file upload)
      const reader = new FileReader();
      reader.onload = () => {
        const base64 = reader.result as string;
        onUpload(payout, { ...formData, file_url: base64, file_name: file.name });
      };
      reader.readAsDataURL(file);
    } else {
      onUpload(payout, formData);
    }
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
      <div className="bg-white rounded-lg p-6 w-full max-w-md">
        <h3 className="text-lg font-semibold mb-4">Upload Payment Proof</h3>
        <form onSubmit={handleSubmit}>
          <div className="mb-4">
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Payment Reference *
            </label>
            <input
              type="text"
              value={formData.reference}
              onChange={(e) => setFormData({ ...formData, reference: e.target.value })}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-purple-500 focus:border-transparent"
              placeholder="e.g., WIRE-123456"
              required
            />
          </div>

          <div className="mb-4">
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Payment Method
            </label>
            <select
              value={formData.method}
              onChange={(e) => setFormData({ ...formData, method: e.target.value })}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-purple-500 focus:border-transparent"
            >
              <option value="wire">Wire Transfer</option>
              <option value="ach">ACH</option>
              <option value="paypal">PayPal</option>
              <option value="other">Other</option>
            </select>
          </div>

          <div className="mb-4">
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Bank/Provider Name
            </label>
            <input
              type="text"
              value={formData.provider}
              onChange={(e) => setFormData({ ...formData, provider: e.target.value })}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-purple-500 focus:border-transparent"
              placeholder="e.g., Chase Bank"
            />
          </div>

          <div className="mb-4">
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Payment Proof File
            </label>
            <div className="space-y-2">
              <input
                type="file"
                onChange={handleFileChange}
                accept="image/*,.pdf,.doc,.docx"
                className="block w-full text-sm text-gray-500 file:mr-4 file:py-2 file:px-4 file:rounded-lg file:border-0 file:text-sm file:font-semibold file:bg-purple-50 file:text-purple-700 hover:file:bg-purple-100"
              />
              {file && (
                <p className="text-sm text-gray-600">
                  Selected: {file.name}
                </p>
              )}
              {!file && (
                <div className="text-sm">
                  <label className="text-gray-700 mb-1 block">Or enter URL:</label>
                  <input
                    type="url"
                    value={formData.file_url}
                    onChange={(e) => setFormData({ ...formData, file_url: e.target.value })}
                    className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-purple-500 focus:border-transparent text-sm"
                    placeholder="https://..."
                  />
                </div>
              )}
            </div>
          </div>

          <div className="flex justify-end gap-3 mt-6">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={uploading}
              className="px-4 py-2 bg-purple-600 text-white rounded-lg hover:bg-purple-700 disabled:opacity-50"
            >
              {uploading ? 'Uploading...' : 'Upload Proof'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// Create Payout Modal Component
function CreatePayoutModal({ onClose, onSuccess }: any) {
  const [items, setItems] = useState([
    { agent_id: '', amount: '', period_start: '', period_end: '' }
  ]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    
    try {
      const merchantId = localStorage.getItem('merchant_id');
      if (!merchantId) return;

      const validItems = items.filter(item => 
        item.agent_id && item.amount && item.period_start && item.period_end
      ).map(item => ({
        ...item,
        amount: parseFloat(item.amount)
      }));

      if (validItems.length === 0) {
        alert('Please add at least one valid payout item');
        return;
      }

      await apiClient.post(`/merchants/${merchantId}/payouts/bulk`, {
        items: validItems
      });

      alert(`Created ${validItems.length} payouts successfully!`);
      onSuccess();
    } catch (error: any) {
      console.error('Failed to create payouts:', error);
      alert(error.response?.data?.detail || 'Failed to create payouts');
    }
  };

  const addItem = () => {
    setItems([...items, { agent_id: '', amount: '', period_start: '', period_end: '' }]);
  };

  const removeItem = (index: number) => {
    setItems(items.filter((_, i) => i !== index));
  };

  const updateItem = (index: number, field: string, value: string) => {
    const updated = [...items];
    updated[index] = { ...updated[index], [field]: value };
    setItems(updated);
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
      <div className="bg-white rounded-lg p-6 w-full max-w-3xl max-h-[80vh] overflow-y-auto">
        <h3 className="text-lg font-semibold mb-4">Create Bulk Payouts</h3>
        
        <form onSubmit={handleSubmit}>
          <div className="space-y-4 mb-6">
            {items.map((item, index) => (
              <div key={index} className="border border-gray-200 rounded-lg p-4">
                <div className="grid grid-cols-1 md:grid-cols-5 gap-4">
                  <div>
                    <label className="block text-xs font-medium text-gray-700 mb-1">
                      Agent ID
                    </label>
                    <input
                      type="text"
                      value={item.agent_id}
                      onChange={(e) => updateItem(index, 'agent_id', e.target.value)}
                      className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
                      placeholder="agent@email.com"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-gray-700 mb-1">
                      Amount
                    </label>
                    <input
                      type="number"
                      step="0.01"
                      value={item.amount}
                      onChange={(e) => updateItem(index, 'amount', e.target.value)}
                      className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
                      placeholder="100.00"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-gray-700 mb-1">
                      Period Start
                    </label>
                    <input
                      type="date"
                      value={item.period_start}
                      onChange={(e) => updateItem(index, 'period_start', e.target.value)}
                      className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-gray-700 mb-1">
                      Period End
                    </label>
                    <input
                      type="date"
                      value={item.period_end}
                      onChange={(e) => updateItem(index, 'period_end', e.target.value)}
                      className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
                    />
                  </div>
                  <div className="flex items-end">
                    <button
                      type="button"
                      onClick={() => removeItem(index)}
                      className="text-red-600 hover:text-red-800 text-sm font-medium"
                    >
                      Remove
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>

          <button
            type="button"
            onClick={addItem}
            className="mb-6 text-purple-600 hover:text-purple-800 font-medium text-sm"
          >
            + Add Another Payout
          </button>

          <div className="flex justify-end gap-3">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200"
            >
              Cancel
            </button>
            <button
              type="submit"
              className="px-4 py-2 bg-purple-600 text-white rounded-lg hover:bg-purple-700"
            >
              Create Payouts
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
// Agent Bank Details Modal Component
function AgentBankDetailsModal({ bankData, onClose }: any) {
  const { status, agent, bank_details, sharing_enabled, message, instructions } = bankData;

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text);
    alert('✅ Copied!');
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
      <div className="bg-white rounded-lg p-6 w-full max-w-2xl max-h-[80vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold flex items-center gap-2">
            <CreditCard className="w-5 h-5 text-purple-600" />
            Agent Bank Details
          </h3>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-700 text-2xl leading-none">&times;</button>
        </div>

        <div className="mb-6 p-4 bg-gray-50 rounded-lg">
          <p className="text-sm font-medium text-gray-900 mb-2">Agent Information</p>
          <p className="text-sm text-gray-700">Name: {agent?.name || 'N/A'}</p>
          <p className="text-sm text-gray-700">Email: {agent?.email}</p>
          <p className="text-sm text-gray-600 font-mono mt-1">ID: {agent?.agent_id}</p>
        </div>

        {status === 'not_configured' && (
          <div className="p-4 bg-yellow-50 border border-yellow-200 rounded-lg">
            <div className="flex items-start gap-3">
              <AlertCircle className="w-5 h-5 text-yellow-600 mt-0.5" />
              <div>
                <p className="text-sm font-medium text-yellow-900">Bank Account Not Set Up</p>
                <p className="text-sm text-yellow-700 mt-1">{message}</p>
                <p className="text-sm text-yellow-700 mt-2">
                  Contact <strong>{agent?.email}</strong> to obtain bank details.
                </p>
              </div>
            </div>
          </div>
        )}

        {status === 'not_shared' && (
          <div className="p-4 bg-blue-50 border border-blue-200 rounded-lg">
            <div className="flex items-start gap-3">
              <AlertCircle className="w-5 h-5 text-blue-600 mt-0.5" />
              <div>
                <p className="text-sm font-medium text-blue-900">Bank Details Not Shared</p>
                <p className="text-sm text-blue-700 mt-1">{instructions}</p>
                {bank_details && (
                  <div className="mt-3 p-3 bg-white rounded border border-blue-200">
                    <p className="text-sm text-gray-700">Method: <span className="font-medium capitalize">{bank_details.method?.replace('_', ' ')}</span></p>
                    <p className="text-sm text-gray-700">Currency: <span className="font-medium">{bank_details.currency}</span></p>
                  </div>
                )}
              </div>
            </div>
          </div>
        )}

        {status === 'success' && sharing_enabled && bank_details && (
          <div>
            <div className="mb-4 p-3 bg-green-50 border border-green-200 rounded-lg">
              <p className="text-sm text-green-800">✅ Bank details available for payment</p>
            </div>

            <div className="space-y-4">
              <div className="p-4 bg-gray-50 rounded-lg space-y-3">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Account Holder</label>
                  <div className="flex items-center gap-2">
                    <p className="text-base font-semibold text-gray-900">{bank_details.account_holder_name}</p>
                    <button onClick={() => copyToClipboard(bank_details.account_holder_name)} className="text-gray-500 hover:text-gray-700">
                      <Copy className="w-4 h-4" />
                    </button>
                  </div>
                </div>

                {(bank_details.iban || bank_details.iban_preview) && (
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">IBAN</label>
                    <div className="flex items-center gap-2">
                      <code className="text-sm bg-white px-3 py-2 rounded border font-mono">{bank_details.iban || bank_details.iban_preview}</code>
                      <button onClick={() => copyToClipboard(bank_details.iban || bank_details.iban_preview)} className="text-gray-500 hover:text-gray-700">
                        <Copy className="w-4 h-4" />
                      </button>
                    </div>
                  </div>
                )}

                {(bank_details.account_number || bank_details.account_number_last4) && (
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">Account Number</label>
                    <div className="flex items-center gap-2">
                      <code className="text-sm bg-white px-3 py-2 rounded border font-mono">
                        {bank_details.account_number || `****${bank_details.account_number_last4}`}
                      </code>
                      {bank_details.account_number && (
                        <button onClick={() => copyToClipboard(bank_details.account_number)} className="text-gray-500 hover:text-gray-700">
                          <Copy className="w-4 h-4" />
                        </button>
                      )}
                    </div>
                  </div>
                )}

                {bank_details.swift_bic && (
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">SWIFT/BIC</label>
                    <div className="flex items-center gap-2">
                      <code className="text-sm bg-white px-3 py-2 rounded border font-mono">{bank_details.swift_bic}</code>
                      <button onClick={() => copyToClipboard(bank_details.swift_bic)} className="text-gray-500 hover:text-gray-700">
                        <Copy className="w-4 h-4" />
                      </button>
                    </div>
                  </div>
                )}

                {bank_details.routing_number && (
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">Routing Number</label>
                    <div className="flex items-center gap-2">
                      <code className="text-sm bg-white px-3 py-2 rounded border font-mono">{bank_details.routing_number}</code>
                      <button onClick={() => copyToClipboard(bank_details.routing_number)} className="text-gray-500 hover:text-gray-700">
                        <Copy className="w-4 h-4" />
                      </button>
                    </div>
                  </div>
                )}

                {bank_details.bank_name && (
                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <label className="block text-sm font-medium text-gray-700 mb-1">Bank</label>
                      <p className="text-sm text-gray-900">{bank_details.bank_name}</p>
                    </div>
                    {bank_details.bank_country && (
                      <div>
                        <label className="block text-sm font-medium text-gray-700 mb-1">Country</label>
                        <p className="text-sm text-gray-900">{bank_details.bank_country}</p>
                      </div>
                    )}
                  </div>
                )}
              </div>

              <div className="p-3 bg-yellow-50 border border-yellow-200 rounded-lg">
                <p className="text-xs text-yellow-800">
                  <strong>🔒 Note:</strong> Full details not shown. Contact {agent?.email} if needed.
                </p>
              </div>
            </div>
          </div>
        )}

        <div className="mt-6 flex justify-end gap-3">
          {(status === 'not_shared' || status === 'not_configured') && (
            <a
              href={`mailto:${agent?.email}?subject=Bank Account Details for Commission Payment`}
              className="px-4 py-2 bg-purple-600 text-white rounded-lg hover:bg-purple-700"
            >
              Contact Agent
            </a>
          )}
          <button onClick={onClose} className="px-4 py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200">
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

// Proof Modal Component
function ProofModal({ proofUrl, onClose }: { proofUrl: string; onClose: () => void }) {
  const isBase64 = proofUrl.startsWith('data:');
  const isPDF = proofUrl.includes('application/pdf') || proofUrl.endsWith('.pdf');
  
  const handleDownload = () => {
    if (isBase64) {
      const link = document.createElement('a');
      link.href = proofUrl;
      link.download = `payment-proof-${Date.now()}`;
      link.click();
    } else {
      window.open(proofUrl, '_blank');
    }
  };
  
  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-lg w-full max-w-4xl max-h-[90vh] overflow-hidden flex flex-col">
        <div className="flex items-center justify-between p-4 border-b">
          <h3 className="text-lg font-semibold">Payment Proof</h3>
          <div className="flex items-center gap-2">
            <button
              onClick={handleDownload}
              className="px-3 py-1.5 text-sm bg-purple-600 text-white rounded hover:bg-purple-700"
            >
              <Download className="w-4 h-4 inline mr-1" />
              Download
            </button>
            <button onClick={onClose} className="text-gray-500 hover:text-gray-700 text-2xl leading-none">
              &times;
            </button>
          </div>
        </div>
        
        <div className="flex-1 overflow-auto p-4 bg-gray-50">
          {isPDF ? (
            <div className="bg-white rounded p-4 text-center">
              <FileText className="w-16 h-16 mx-auto text-gray-400 mb-4" />
              <p className="text-gray-600 mb-4">PDF Document</p>
              <button onClick={handleDownload} className="px-4 py-2 bg-purple-600 text-white rounded hover:bg-purple-700">
                Download to View
              </button>
            </div>
          ) : (
            <div className="bg-white rounded p-2">
              <img src={proofUrl} alt="Payment Proof" className="max-w-full h-auto mx-auto" />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
