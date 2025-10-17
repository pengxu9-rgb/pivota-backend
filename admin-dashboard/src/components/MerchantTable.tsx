import { useState } from 'react';
import { Merchant, merchantApi, integrationsApi } from '../lib/api';
import { Search, RefreshCw, Eye, FileCheck, Upload, Trash2, ExternalLink, Link, Boxes } from 'lucide-react';
import { formatDate } from '../lib/utils';
import MerchantDetailsModal from './MerchantDetailsModal';
import KYBReviewModal from './KYBReviewModal';
import UploadDocsModal from './UploadDocsModal';

interface MerchantTableProps {
  merchants: Merchant[];
  loading: boolean;
  onRefresh: () => void;
}

export default function MerchantTable({ merchants, loading, onRefresh }: MerchantTableProps) {
  const [searchText, setSearchText] = useState('');
  const [statusFilter, setStatusFilter] = useState('all');
  const [selectedMerchant, setSelectedMerchant] = useState<Merchant | null>(null);
  const [showDetailsModal, setShowDetailsModal] = useState(false);
  const [showKYBModal, setShowKYBModal] = useState(false);
  const [showUploadModal, setShowUploadModal] = useState(false);

  const filteredMerchants = merchants.filter((m) => {
    const matchesSearch =
      searchText === '' ||
      m.business_name?.toLowerCase().includes(searchText.toLowerCase()) ||
      m.contact_email?.toLowerCase().includes(searchText.toLowerCase()) ||
      m.merchant_id?.toLowerCase().includes(searchText.toLowerCase());

    const matchesStatus = statusFilter === 'all' || m.status === statusFilter;

    return matchesSearch && matchesStatus;
  });

  const handleDelete = async (merchant: Merchant) => {
    if (!confirm(`Delete merchant "${merchant.business_name}"?`)) return;

    try {
      await merchantApi.delete(merchant.merchant_id);
      alert('Merchant deleted successfully!');
      onRefresh();
    } catch (error: any) {
      alert('Failed to delete merchant: ' + (error.response?.data?.detail || error.message));
    }
  };

  const getBadgeColor = (status: string) => {
    switch (status) {
      case 'approved':
        return 'bg-green-100 text-green-800';
      case 'pending_verification':
        return 'bg-yellow-100 text-yellow-800';
      case 'rejected':
        return 'bg-red-100 text-red-800';
      default:
        return 'bg-slate-100 text-slate-800';
    }
  };

  return (
    <div className="bg-white rounded-xl shadow-sm border border-slate-200">
      {/* Toolbar */}
      <div className="p-4 border-b border-slate-200 flex flex-wrap gap-4 items-center justify-between">
        <div className="flex gap-3 flex-1">
          <div className="relative flex-1 max-w-sm">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
            <input
              type="text"
              placeholder="Search merchants..."
              value={searchText}
              onChange={(e) => setSearchText(e.target.value)}
              className="w-full pl-10 pr-4 py-2 text-sm border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent"
            />
          </div>

          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="px-4 py-2 text-sm border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent"
          >
            <option value="all">All Status</option>
            <option value="approved">Approved</option>
            <option value="pending_verification">Pending</option>
            <option value="rejected">Rejected</option>
          </select>
        </div>

        <button
          onClick={onRefresh}
          disabled={loading}
          className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-slate-700 bg-white border border-slate-300 rounded-lg hover:bg-slate-50 disabled:opacity-50"
        >
          <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      {/* Table */}
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead className="bg-slate-50 border-b border-slate-200">
            <tr>
              <th className="px-4 py-3 text-left text-xs font-medium text-slate-600 uppercase">
                Merchant
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium text-slate-600 uppercase">
                Store URL
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium text-slate-600 uppercase">
                Status
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium text-slate-600 uppercase">
                PSP
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium text-slate-600 uppercase">
                MCP
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium text-slate-600 uppercase">
                Auto
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium text-slate-600 uppercase">
                Confidence
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium text-slate-600 uppercase">
                Created
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium text-slate-600 uppercase">
                Actions
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-200">
            {loading ? (
              <tr>
                <td colSpan={7} className="px-4 py-8 text-center text-sm text-slate-500">
                  Loading merchants...
                </td>
              </tr>
            ) : filteredMerchants.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-4 py-8 text-center text-sm text-slate-500">
                  No merchants found
                </td>
              </tr>
            ) : (
              filteredMerchants.map((merchant) => (
                <tr key={merchant.merchant_id} className="hover:bg-slate-50">
                  <td className="px-4 py-3">
                    <div>
                      <div className="text-sm font-medium text-slate-900">
                        {merchant.business_name}
                      </div>
                      <div className="text-xs text-slate-500">{merchant.merchant_id}</div>
                      <div className="text-xs text-slate-500">{merchant.contact_email}</div>
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    {merchant.store_url ? (
                      <a
                        href={merchant.store_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-sm text-primary hover:underline flex items-center gap-1"
                      >
                        {merchant.store_url.replace(/^https?:\/\//, '').slice(0, 30)}...
                        <ExternalLink className="w-3 h-3" />
                      </a>
                    ) : (
                      <span className="text-sm text-slate-400">N/A</span>
                    )}
                  </td>
                <td className="px-4 py-3">
                    <span
                      className={`inline-flex px-2 py-1 text-xs font-medium rounded-full ${getBadgeColor(
                        merchant.status
                      )}`}
                    >
                      {merchant.status}
                    </span>
                  </td>
                <td className="px-4 py-3 text-sm text-slate-700">
                  {merchant.psp_connected ? (
                    <span className="inline-flex items-center gap-1 text-green-700">
                      ✓ {merchant.psp_type || 'PSP'}
                    </span>
                  ) : (
                    <span className="text-slate-400">-</span>
                  )}
                </td>
                <td className="px-4 py-3 text-sm text-slate-700">
                  {merchant.mcp_connected ? (
                    <span className="inline-flex items-center gap-1 text-green-700">
                      ✓ {merchant.mcp_platform || 'MCP'}
                    </span>
                  ) : (
                    <span className="text-slate-400">-</span>
                  )}
                </td>
                  <td className="px-4 py-3">
                    <span className="text-sm text-slate-700">
                      {merchant.auto_approved ? '✓' : '-'}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <span className="text-sm text-slate-700">
                      {merchant.approval_confidence
                        ? `${(merchant.approval_confidence * 100).toFixed(0)}%`
                        : '-'}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <span className="text-sm text-slate-700">{formatDate(merchant.created_at)}</span>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      <button
                        onClick={() => {
                          setSelectedMerchant(merchant);
                          setShowDetailsModal(true);
                        }}
                        className="p-1.5 text-slate-600 hover:text-primary hover:bg-slate-100 rounded"
                        title="View Details"
                      >
                        <Eye className="w-4 h-4" />
                      </button>
                      <button
                        onClick={() => {
                          setSelectedMerchant(merchant);
                          setShowKYBModal(true);
                        }}
                        className="p-1.5 text-slate-600 hover:text-blue-600 hover:bg-blue-50 rounded"
                        title="Review KYB"
                      >
                        <FileCheck className="w-4 h-4" />
                      </button>
                      <button
                        onClick={() => {
                          setSelectedMerchant(merchant);
                          setShowUploadModal(true);
                        }}
                        className="p-1.5 text-slate-600 hover:text-green-600 hover:bg-green-50 rounded"
                        title="Upload Docs"
                      >
                        <Upload className="w-4 h-4" />
                      </button>
                      <button
                        onClick={async () => {
                          try {
                            await integrationsApi.connectShopify(merchant.merchant_id);
                            alert('Shopify connected!');
                            onRefresh();
                          } catch (e: any) {
                            alert('Connect failed: ' + (e.response?.data?.detail || e.message));
                          }
                        }}
                        className="p-1.5 text-slate-600 hover:text-emerald-600 hover:bg-emerald-50 rounded"
                        title="Connect Shopify"
                      >
                        <Link className="w-4 h-4" />
                      </button>
                      <button
                        onClick={async () => {
                          try {
                            const res = await integrationsApi.syncShopifyProducts(merchant.merchant_id, 20);
                            console.log('Products:', res);
                            alert('Sync started (see console for result)');
                          } catch (e: any) {
                            alert('Sync failed: ' + (e.response?.data?.detail || e.message));
                          }
                        }}
                        className="p-1.5 text-slate-600 hover:text-indigo-600 hover:bg-indigo-50 rounded"
                        title="Sync Products"
                      >
                        <Boxes className="w-4 h-4" />
                      </button>
                      <button
                        onClick={() => handleDelete(merchant)}
                        className="p-1.5 text-slate-600 hover:text-red-600 hover:bg-red-50 rounded"
                        title="Delete"
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Modals */}
      {selectedMerchant && (
        <>
          <MerchantDetailsModal
            merchant={selectedMerchant}
            open={showDetailsModal}
            onClose={() => setShowDetailsModal(false)}
          />
          <KYBReviewModal
            merchant={selectedMerchant}
            open={showKYBModal}
            onClose={() => {
              setShowKYBModal(false);
              onRefresh();
            }}
          />
          <UploadDocsModal
            merchant={selectedMerchant}
            open={showUploadModal}
            onClose={() => {
              setShowUploadModal(false);
              onRefresh();
            }}
          />
        </>
      )}
    </div>
  );
}

