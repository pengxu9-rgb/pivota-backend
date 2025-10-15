import React, { useState } from 'react';
import { X, CheckCircle, XCircle, FileText, Download } from 'lucide-react';

interface KYBReviewModalProps {
  isOpen: boolean;
  onClose: () => void;
  merchant: any;
  onApprove: (merchantId: string) => Promise<void>;
  onReject: (merchantId: string, reason: string) => Promise<void>;
}

export const KYBReviewModal: React.FC<KYBReviewModalProps> = ({
  isOpen,
  onClose,
  merchant,
  onApprove,
  onReject
}) => {
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [rejectionReason, setRejectionReason] = useState('');
  const [showRejectForm, setShowRejectForm] = useState(false);

  const handleApprove = async () => {
    setIsSubmitting(true);
    try {
      await onApprove(merchant.id);
      onClose();
    } catch (err) {
      console.error('Approval failed:', err);
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleReject = async () => {
    if (!rejectionReason.trim()) {
      alert('Please provide a reason for rejection');
      return;
    }
    
    setIsSubmitting(true);
    try {
      await onReject(merchant.id, rejectionReason);
      onClose();
      setRejectionReason('');
      setShowRejectForm(false);
    } catch (err) {
      console.error('Rejection failed:', err);
    } finally {
      setIsSubmitting(false);
    }
  };

  if (!isOpen || !merchant) return null;

  const documents = merchant.kyb_documents || [];

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
            <h2 className="text-2xl font-bold">KYB Review: {merchant.name}</h2>
            <p className="text-sm text-gray-600 mt-1">
              Platform: {merchant.platform} • Status: {merchant.verification_status}
            </p>
          </div>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-700">
            <X size={24} />
          </button>
        </div>

        <div className="p-6 space-y-6">
          {/* Merchant Information */}
          <div className="card">
            <h3 className="text-lg font-semibold mb-4">Merchant Information</h3>
            <div className="grid grid-cols-2 gap-4 text-sm">
              <div>
                <span className="font-medium text-gray-700">Business Name:</span>
                <p className="text-gray-900 mt-1">{merchant.name}</p>
              </div>
              <div>
                <span className="font-medium text-gray-700">Platform:</span>
                <p className="text-gray-900 mt-1 capitalize">{merchant.platform}</p>
              </div>
              <div>
                <span className="font-medium text-gray-700">Store URL:</span>
                <a 
                  href={merchant.store_url} 
                  target="_blank" 
                  rel="noopener noreferrer"
                  className="text-blue-600 hover:underline mt-1 block"
                >
                  {merchant.store_url}
                </a>
              </div>
              <div>
                <span className="font-medium text-gray-700">Status:</span>
                <p className="mt-1">
                  <span className={`px-2 py-1 rounded text-xs font-medium ${
                    merchant.status === 'approved' ? 'bg-green-100 text-green-800' :
                    merchant.status === 'in_progress' ? 'bg-yellow-100 text-yellow-800' :
                    'bg-red-100 text-red-800'
                  }`}>
                    {merchant.status}
                  </span>
                </p>
              </div>
              <div>
                <span className="font-medium text-gray-700">Volume Processed:</span>
                <p className="text-gray-900 mt-1">€{merchant.volume_processed?.toLocaleString()}</p>
              </div>
              <div>
                <span className="font-medium text-gray-700">Last Activity:</span>
                <p className="text-gray-900 mt-1">
                  {new Date(merchant.last_activity).toLocaleDateString()}
                </p>
              </div>
            </div>
          </div>

          {/* KYB Documents */}
          <div className="card">
            <h3 className="text-lg font-semibold mb-4">
              KYB Documents ({documents.length})
            </h3>
            
            {documents.length === 0 ? (
              <p className="text-gray-500 text-center py-8">
                No documents uploaded yet
              </p>
            ) : (
              <div className="space-y-3">
                {documents.map((doc: any, index: number) => (
                  <div key={index} className="flex items-center justify-between p-3 bg-gray-50 rounded">
                    <div className="flex items-center gap-3">
                      <FileText className="text-blue-500" size={20} />
                      <div>
                        <p className="font-medium text-sm">{doc.name || `Document ${index + 1}`}</p>
                        <p className="text-xs text-gray-500">{doc.type || 'PDF'} • {doc.size || 'Unknown size'}</p>
                      </div>
                    </div>
                    <button className="text-blue-600 hover:text-blue-700">
                      <Download size={18} />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Verification Checklist */}
          <div className="card">
            <h3 className="text-lg font-semibold mb-4">Verification Checklist</h3>
            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <CheckCircle className="text-green-500" size={20} />
                <span className="text-sm">Business registration verified</span>
              </div>
              <div className="flex items-center gap-2">
                <CheckCircle className="text-green-500" size={20} />
                <span className="text-sm">Identity documents submitted</span>
              </div>
              <div className="flex items-center gap-2">
                <CheckCircle className="text-green-500" size={20} />
                <span className="text-sm">Business address confirmed</span>
              </div>
              <div className="flex items-center gap-2">
                {merchant.verification_status === 'verified' ? (
                  <CheckCircle className="text-green-500" size={20} />
                ) : (
                  <XCircle className="text-gray-400" size={20} />
                )}
                <span className="text-sm">Final compliance review</span>
              </div>
            </div>
          </div>

          {/* Decision Section */}
          {merchant.status !== 'approved' && (
            <div className="card bg-gray-50">
              <h3 className="text-lg font-semibold mb-4">Review Decision</h3>
              
              {!showRejectForm ? (
                <div className="flex gap-3">
                  <button
                    onClick={handleApprove}
                    disabled={isSubmitting}
                    className="btn btn-success flex-1 flex items-center justify-center gap-2"
                  >
                    <CheckCircle size={18} />
                    {isSubmitting ? 'Approving...' : 'Approve Merchant'}
                  </button>
                  <button
                    onClick={() => setShowRejectForm(true)}
                    disabled={isSubmitting}
                    className="btn btn-danger flex-1 flex items-center justify-center gap-2"
                  >
                    <XCircle size={18} />
                    Reject
                  </button>
                </div>
              ) : (
                <div className="space-y-3">
                  <label className="block text-sm font-medium text-gray-700">
                    Reason for Rejection *
                  </label>
                  <textarea
                    value={rejectionReason}
                    onChange={(e) => setRejectionReason(e.target.value)}
                    rows={3}
                    className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                    placeholder="Please provide a detailed reason for rejection..."
                  />
                  <div className="flex gap-3">
                    <button
                      onClick={() => {
                        setShowRejectForm(false);
                        setRejectionReason('');
                      }}
                      className="btn btn-secondary flex-1"
                    >
                      Cancel
                    </button>
                    <button
                      onClick={handleReject}
                      disabled={isSubmitting || !rejectionReason.trim()}
                      className="btn btn-danger flex-1"
                    >
                      {isSubmitting ? 'Rejecting...' : 'Confirm Rejection'}
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}

          {merchant.status === 'approved' && (
            <div className="card bg-green-50 border border-green-200">
              <div className="flex items-center gap-3">
                <CheckCircle className="text-green-600" size={24} />
                <div>
                  <p className="font-semibold text-green-900">Merchant Approved</p>
                  <p className="text-sm text-green-700">This merchant has been verified and approved for payments.</p>
                </div>
              </div>
            </div>
          )}
        </div>

        <div className="sticky bottom-0 bg-gray-50 border-t border-gray-200 p-4">
          <button onClick={onClose} className="btn btn-secondary w-full">
            Close
          </button>
        </div>
    </div>
  );
};

