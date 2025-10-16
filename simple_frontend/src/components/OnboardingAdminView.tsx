/**
 * OnboardingAdminView - Admin view for Phase 2 merchant onboarding
 * Shows all merchant onboarding records with KYC approval/rejection
 */

import React, { useState, useEffect } from 'react';
import { api, merchantApi } from '../services/api';
import { CheckCircle, XCircle, Clock, Upload, Eye, FileText, Trash2 } from 'lucide-react';

interface OnboardingMerchant {
  merchant_id: string;
  business_name: string;
  kyc_status: string;
  psp_connected: boolean;
  psp_type?: string;
  created_at: string;
  website?: string;
  contact_email?: string;
  status?: string;
}

interface OnboardingAdminViewProps {
  onUploadDocs?: (merchantId: string) => void;
  onReviewKYB?: (merchant: OnboardingMerchant) => void;
  onViewDetails?: (merchant: OnboardingMerchant) => void;
  onRemove?: (merchant: OnboardingMerchant) => Promise<boolean>;
}

export const OnboardingAdminView: React.FC<OnboardingAdminViewProps> = ({
  onUploadDocs,
  onReviewKYB,
  onViewDetails,
  onRemove
}) => {
  const [merchants, setMerchants] = useState<OnboardingMerchant[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selectedFilter, setSelectedFilter] = useState<string>('all');
  const [resumeId, setResumeId] = useState('');

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

  // Apply filter to Phase 2 merchants only
  const filteredMerchants = selectedFilter === 'all' 
    ? merchants 
    : merchants.filter(m => m.kyc_status === selectedFilter || m.status === selectedFilter);

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
      {filteredMerchants.length === 0 ? (
        <div className="text-center py-12 bg-gray-50 rounded space-y-4">
          <p className="text-gray-600">No merchants found for this filter.</p>
          {selectedFilter === 'all' && (
            <div className="max-w-xl mx-auto flex flex-col md:flex-row gap-2 items-stretch md:items-end">
              <div className="flex-1 text-left">
                <label className="block text-sm font-medium mb-1">Resume with Merchant ID</label>
                <input
                  type="text"
                  value={resumeId}
                  onChange={(e) => setResumeId(e.target.value)}
                  className="w-full px-3 py-2 border rounded font-mono"
                  placeholder="merch_..."
                />
              </div>
              <button
                onClick={() => {
                  if (!resumeId.trim()) return;
                  try { localStorage.setItem('merchant_onboarding_id', resumeId.trim()); } catch {}
                  window.location.href = '/merchant/onboarding';
                }}
                className="btn btn-primary md:w-auto"
              >
                Resume PSP Setup
              </button>
            </div>
          )}
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {filteredMerchants.map((merchant) => (
            <div key={merchant.merchant_id} className="card">
              <div className="flex justify-between items-start mb-3">
                <div>
                  <h3 className="text-lg font-semibold">{merchant.business_name}</h3>
                  <p className="text-sm text-gray-500">ID: {merchant.merchant_id}</p>
                  {merchant.website && (
                    <a 
                      href={merchant.website} 
                      target="_blank" 
                      rel="noopener noreferrer"
                      className="text-xs text-blue-600 hover:underline"
                    >
                      {merchant.website}
                    </a>
                  )}
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

              {/* Action Buttons */}
              <div className="space-y-2">
                {/* Primary Actions for Pending */}
                {merchant.kyc_status === 'pending_verification' && (
                  <div className="flex gap-2">
                    <button
                      onClick={() => handleApprove(merchant.merchant_id)}
                      className="btn btn-success btn-sm flex-1"
                    >
                      <CheckCircle size={14} className="inline mr-1" />
                      Approve
                    </button>
                    <button
                      onClick={() => handleReject(merchant.merchant_id)}
                      className="btn btn-danger btn-sm flex-1"
                    >
                      <XCircle size={14} className="inline mr-1" />
                      Reject
                    </button>
                  </div>
                )}

                {/* PSP Setup Reminder */}
                {merchant.kyc_status === 'approved' && !merchant.psp_connected && (
                  <div className="bg-blue-50 border border-blue-200 p-2 rounded text-sm text-blue-700 mb-2">
                    ℹ️ Waiting for merchant to connect PSP
                  </div>
                )}

                {/* Secondary Actions - Always visible */}
                <div className="grid grid-cols-2 gap-2">
                  {onUploadDocs && (
                    <button
                      onClick={() => onUploadDocs(merchant.merchant_id)}
                      className="btn btn-secondary btn-sm text-xs"
                      title="Upload Documents"
                    >
                      <Upload size={14} className="inline mr-1" />
                      Upload Docs
                    </button>
                  )}
                  
                  {onReviewKYB && (
                    <button
                      onClick={() => onReviewKYB(merchant)}
                      className="btn btn-primary btn-sm text-xs"
                      title="Review KYB"
                    >
                      <Eye size={14} className="inline mr-1" />
                      Review KYB
                    </button>
                  )}
                  
                  {onViewDetails && (
                    <button
                      onClick={() => onViewDetails(merchant)}
                      className="btn btn-secondary btn-sm text-xs"
                      title="View Details"
                    >
                      <FileText size={14} className="inline mr-1" />
                      Details
                    </button>
                  )}
                  
                  {onRemove && (
                    <button
                      onClick={async () => {
                        if (window.confirm(`Remove merchant "${merchant.business_name}"?`)) {
                          const success = await onRemove(merchant);
                          if (success) {
                            // Reload merchant list after successful deletion
                            loadMerchants();
                          }
                        }
                      }}
                      className="btn btn-danger btn-sm text-xs"
                      title="Remove Merchant"
                    >
                      <Trash2 size={14} className="inline mr-1" />
                      Remove
                    </button>
                  )}
                </div>

                {/* Resume PSP Setup */}
                {merchant.kyc_status === 'approved' && !merchant.psp_connected && (
                  <button
                    onClick={() => {
                      try {
                        localStorage.setItem('merchant_onboarding_id', merchant.merchant_id);
                      } catch {}
                      window.location.href = '/merchant/onboarding';
                    }}
                    className="btn btn-primary btn-sm w-full"
                  >
                    Resume PSP Setup
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

