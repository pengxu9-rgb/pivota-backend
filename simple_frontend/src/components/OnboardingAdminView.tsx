/**
 * OnboardingAdminView - Admin view for Phase 2 merchant onboarding
 * Shows all merchant onboarding records with KYC approval/rejection
 */

import React, { useState, useEffect } from 'react';
import { api } from '../services/api';
import { CheckCircle, XCircle, Clock } from 'lucide-react';

interface OnboardingMerchant {
  merchant_id: string;
  business_name: string;
  kyc_status: string;
  psp_connected: boolean;
  psp_type?: string;
  created_at: string;
}

export const OnboardingAdminView: React.FC = () => {
  const [merchants, setMerchants] = useState<OnboardingMerchant[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selectedFilter, setSelectedFilter] = useState<string>('all');

  useEffect(() => {
    loadMerchants();
  }, [selectedFilter]);

  const loadMerchants = async () => {
    try {
      setLoading(true);
      setError('');
      const filterParam = selectedFilter !== 'all' ? `?status=${selectedFilter}` : '';
      const response = await api.get(`/merchant/onboarding/all${filterParam}`);
      setMerchants(response.data.merchants || []);
    } catch (err: any) {
      console.error('Failed to load onboarding merchants:', err);
      const errorMsg = err.response?.data?.detail || err.message || 'Failed to load merchants';
      
      if (err.response?.status === 401 || errorMsg.includes('Not authenticated')) {
        setError('Authentication required. Please login as admin.');
      } else if (err.response?.status === 500) {
        setError('Server error. The backend may still be starting up. Please wait a moment and refresh.');
      } else {
        setError(errorMsg);
      }
    } finally {
      setLoading(false);
    }
  };

  const handleApprove = async (merchantId: string) => {
    if (!window.confirm('Approve this merchant?')) return;

    try {
      await api.post(`/merchant/onboarding/approve/${merchantId}`);
      alert('✅ Merchant approved successfully!');
      loadMerchants();
    } catch (err: any) {
      alert(`❌ Failed to approve: ${err.response?.data?.detail || err.message}`);
    }
  };

  const handleReject = async (merchantId: string) => {
    const reason = window.prompt('Enter rejection reason:');
    if (!reason) return;

    try {
      await api.post(`/merchant/onboarding/reject/${merchantId}?reason=${encodeURIComponent(reason)}`);
      alert('✅ Merchant rejected');
      loadMerchants();
    } catch (err: any) {
      alert(`❌ Failed to reject: ${err.response?.data?.detail || err.message}`);
    }
  };

  if (loading) {
    return <div className="text-center py-8">Loading onboarding merchants...</div>;
  }

  if (error) {
    return (
      <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded">
        {error}
      </div>
    );
  }

  return (
    <div>
      {/* Filter Tabs */}
      <div className="mb-4 border-b">
        <nav className="flex space-x-4">
          {['all', 'pending_verification', 'approved', 'rejected'].map((filter) => (
            <button
              key={filter}
              onClick={() => setSelectedFilter(filter)}
              className={`pb-2 px-1 border-b-2 transition-colors ${
                selectedFilter === filter
                  ? 'border-blue-500 text-blue-600'
                  : 'border-transparent text-gray-500 hover:text-gray-700'
              }`}
            >
              {filter === 'all' ? 'All' : filter.replace('_', ' ').replace(/\b\w/g, l => l.toUpperCase())}
            </button>
          ))}
        </nav>
      </div>

      {/* Merchants List */}
      {merchants.length === 0 ? (
        <div className="text-center py-12 bg-gray-50 rounded">
          <p className="text-gray-600">No merchants found</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {merchants.map((merchant) => (
            <div key={merchant.merchant_id} className="card">
              <div className="flex justify-between items-start mb-3">
                <div>
                  <h3 className="text-lg font-semibold">{merchant.business_name}</h3>
                  <p className="text-sm text-gray-500">ID: {merchant.merchant_id}</p>
                </div>
                <div className={`px-2 py-1 rounded text-xs font-medium ${
                  merchant.kyc_status === 'approved' ? 'bg-green-100 text-green-800' :
                  merchant.kyc_status === 'rejected' ? 'bg-red-100 text-red-800' :
                  'bg-yellow-100 text-yellow-800'
                }`}>
                  {merchant.kyc_status}
                </div>
              </div>

              <div className="space-y-2 mb-4">
                <div className="flex items-center text-sm">
                  <span className="text-gray-600 mr-2">PSP:</span>
                  {merchant.psp_connected ? (
                    <span className="flex items-center text-green-600">
                      <CheckCircle size={14} className="mr-1" />
                      {merchant.psp_type || 'Connected'}
                    </span>
                  ) : (
                    <span className="flex items-center text-gray-400">
                      <XCircle size={14} className="mr-1" />
                      Not connected
                    </span>
                  )}
                </div>
                <div className="text-sm text-gray-600">
                  <Clock size={14} className="inline mr-1" />
                  Created: {new Date(merchant.created_at).toLocaleDateString()}
                </div>
              </div>

              {/* Actions */}
              {merchant.kyc_status === 'pending_verification' && (
                <div className="flex gap-2">
                  <button
                    onClick={() => handleApprove(merchant.merchant_id)}
                    className="btn btn-success btn-sm flex-1"
                  >
                    Approve
                  </button>
                  <button
                    onClick={() => handleReject(merchant.merchant_id)}
                    className="btn btn-danger btn-sm flex-1"
                  >
                    Reject
                  </button>
                </div>
              )}

              {merchant.kyc_status === 'approved' && !merchant.psp_connected && (
                <div className="bg-blue-50 border border-blue-200 p-2 rounded text-sm text-blue-700">
                  ℹ️ Waiting for merchant to connect PSP
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

