import React from 'react';
import { X, ExternalLink, Calendar, DollarSign, FileText, Shield } from 'lucide-react';

interface MerchantDetailsModalProps {
  isOpen: boolean;
  onClose: () => void;
  merchant: any;
}

export const MerchantDetailsModal: React.FC<MerchantDetailsModalProps> = ({
  isOpen,
  onClose,
  merchant
}) => {
  if (!isOpen || !merchant) return null;

  return (
    <div 
      className="bg-white rounded-lg w-full overflow-y-auto"
      style={{ 
        zIndex: 1000000,
        maxWidth: '800px',
        maxHeight: '85vh'
      }}
      onClick={(e) => e.stopPropagation()}
    >
        <div className="sticky top-0 bg-white border-b border-gray-200 p-6 flex justify-between items-center">
          <div>
            <h2 className="text-2xl font-bold">{merchant.name}</h2>
            <p className="text-sm text-gray-600 mt-1">Complete Merchant Profile</p>
          </div>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-700">
            <X size={24} />
          </button>
        </div>

        <div className="p-6 space-y-6">
          {/* Status Badge */}
          <div className="flex items-center gap-3">
            <span className={`px-4 py-2 rounded-full text-sm font-medium ${
              merchant.status === 'approved' ? 'bg-green-100 text-green-800' :
              merchant.status === 'in_progress' ? 'bg-yellow-100 text-yellow-800' :
              'bg-red-100 text-red-800'
            }`}>
              {merchant.status}
            </span>
            <span className={`px-4 py-2 rounded-full text-sm font-medium ${
              merchant.verification_status === 'verified' ? 'bg-blue-100 text-blue-800' :
              'bg-gray-100 text-gray-800'
            }`}>
              {merchant.verification_status}
            </span>
          </div>

          {/* Business Information */}
          <div className="card">
            <h3 className="text-lg font-semibold mb-4 flex items-center gap-2">
              <Shield size={20} className="text-blue-500" />
              Business Information
            </h3>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <label className="text-sm font-medium text-gray-500">Business Name</label>
                <p className="text-gray-900 mt-1">{merchant.name}</p>
              </div>
              <div>
                <label className="text-sm font-medium text-gray-500">Platform</label>
                <p className="text-gray-900 mt-1 capitalize">{merchant.platform}</p>
              </div>
              <div>
                <label className="text-sm font-medium text-gray-500">Store URL</label>
                <a 
                  href={merchant.store_url} 
                  target="_blank" 
                  rel="noopener noreferrer"
                  className="text-blue-600 hover:underline mt-1 flex items-center gap-1"
                >
                  {merchant.store_url}
                  <ExternalLink size={14} />
                </a>
              </div>
              <div>
                <label className="text-sm font-medium text-gray-500">Merchant ID</label>
                <p className="text-gray-900 mt-1 font-mono text-sm">{merchant.id}</p>
              </div>
            </div>
          </div>

          {/* Financial Information */}
          <div className="card">
            <h3 className="text-lg font-semibold mb-4 flex items-center gap-2">
              <DollarSign size={20} className="text-green-500" />
              Financial Information
            </h3>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <div>
                <label className="text-sm font-medium text-gray-500">Volume Processed</label>
                <p className="text-2xl font-bold text-gray-900 mt-1">
                  €{merchant.volume_processed?.toLocaleString() || 0}
                </p>
              </div>
              <div>
                <label className="text-sm font-medium text-gray-500">Total Transactions</label>
                <p className="text-2xl font-bold text-gray-900 mt-1">
                  {merchant.total_transactions?.toLocaleString() || 0}
                </p>
              </div>
              <div>
                <label className="text-sm font-medium text-gray-500">Average Transaction</label>
                <p className="text-2xl font-bold text-gray-900 mt-1">
                  €{merchant.avg_transaction_value?.toLocaleString() || 0}
                </p>
              </div>
            </div>
          </div>

          {/* KYB Documents */}
          <div className="card">
            <h3 className="text-lg font-semibold mb-4 flex items-center gap-2">
              <FileText size={20} className="text-purple-500" />
              KYB Documents
            </h3>
            <div className="space-y-2">
              {merchant.kyb_documents && merchant.kyb_documents.length > 0 ? (
                merchant.kyb_documents.map((doc: any, index: number) => (
                  <div key={index} className="flex items-center justify-between p-3 bg-gray-50 rounded">
                    <div>
                      <p className="font-medium text-sm">{doc.name || `Document ${index + 1}`}</p>
                      <p className="text-xs text-gray-500">{doc.type || 'PDF'}</p>
                    </div>
                    <span className="text-xs text-green-600">✓ Verified</span>
                  </div>
                ))
              ) : (
                <p className="text-gray-500 text-center py-4">No documents uploaded</p>
              )}
            </div>
          </div>

          {/* Activity Timeline */}
          <div className="card">
            <h3 className="text-lg font-semibold mb-4 flex items-center gap-2">
              <Calendar size={20} className="text-orange-500" />
              Activity Timeline
            </h3>
            <div className="space-y-3">
              <div className="flex items-start gap-3">
                <div className="w-2 h-2 bg-blue-500 rounded-full mt-2" />
                <div>
                  <p className="text-sm font-medium">Last Activity</p>
                  <p className="text-xs text-gray-500">
                    {new Date(merchant.last_activity).toLocaleString()}
                  </p>
                </div>
              </div>
              <div className="flex items-start gap-3">
                <div className="w-2 h-2 bg-green-500 rounded-full mt-2" />
                <div>
                  <p className="text-sm font-medium">Verification Status Updated</p>
                  <p className="text-xs text-gray-500 capitalize">
                    Status: {merchant.verification_status}
                  </p>
                </div>
              </div>
              <div className="flex items-start gap-3">
                <div className="w-2 h-2 bg-purple-500 rounded-full mt-2" />
                <div>
                  <p className="text-sm font-medium">Merchant Onboarded</p>
                  <p className="text-xs text-gray-500">
                    Platform: {merchant.platform}
                  </p>
                </div>
              </div>
            </div>
          </div>

          {/* Integration Details */}
          <div className="card">
            <h3 className="text-lg font-semibold mb-4">Integration Details</h3>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <label className="text-sm font-medium text-gray-500">API Integration</label>
                <p className="text-gray-900 mt-1">
                  {merchant.platform === 'shopify' ? 'Shopify API' : 
                   merchant.platform === 'wix' ? 'Wix API' : 
                   'Custom Integration'}
                </p>
              </div>
              <div>
                <label className="text-sm font-medium text-gray-500">Webhook Status</label>
                <p className="text-gray-900 mt-1">
                  <span className="inline-flex items-center px-2 py-1 rounded-full text-xs bg-green-100 text-green-800">
                    ✓ Active
                  </span>
                </p>
              </div>
              <div>
                <label className="text-sm font-medium text-gray-500">Payment Methods</label>
                <p className="text-gray-900 mt-1">Stripe, Adyen</p>
              </div>
              <div>
                <label className="text-sm font-medium text-gray-500">Settlement Currency</label>
                <p className="text-gray-900 mt-1">EUR</p>
              </div>
            </div>
          </div>

          {/* Notes */}
          {merchant.notes && (
            <div className="card bg-yellow-50 border border-yellow-200">
              <h3 className="text-lg font-semibold mb-2">Internal Notes</h3>
              <p className="text-sm text-gray-700">{merchant.notes}</p>
            </div>
          )}
        </div>

        <div className="sticky bottom-0 bg-gray-50 border-t border-gray-200 p-4">
          <button onClick={onClose} className="btn btn-primary w-full">
            Close
          </button>
        </div>
    </div>
  );
};

