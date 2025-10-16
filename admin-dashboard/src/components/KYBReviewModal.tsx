import { useState } from 'react';
import { Merchant, merchantApi } from '../lib/api';
import { X, CheckCircle, XCircle } from 'lucide-react';

interface KYBReviewModalProps {
  merchant: Merchant;
  open: boolean;
  onClose: () => void;
}

export default function KYBReviewModal({ merchant, open, onClose }: KYBReviewModalProps) {
  const [decision, setDecision] = useState<'approved' | 'rejected'>('approved');
  const [reason, setReason] = useState('');
  const [submitting, setSubmitting] = useState(false);

  if (!open) return null;

  const handleSubmit = async () => {
    try {
      setSubmitting(true);
      await merchantApi.updateKYB(merchant.merchant_id, decision, reason || undefined);
      alert('KYB status updated successfully!');
      onClose();
    } catch (error: any) {
      alert('Failed to update KYB: ' + (error.response?.data?.detail || error.message));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div
        className="bg-white rounded-xl shadow-2xl max-w-lg w-full"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between p-6 border-b border-slate-200">
          <div>
            <h2 className="text-xl font-bold text-slate-900">KYB Review</h2>
            <p className="text-sm text-slate-500 mt-1">{merchant.business_name}</p>
          </div>
          <button
            onClick={onClose}
            className="p-2 hover:bg-slate-100 rounded-lg transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Content */}
        <div className="p-6 space-y-4">
          {/* Auto-approval Info */}
          {merchant.auto_approved && (
            <div className="p-4 bg-blue-50 border border-blue-200 rounded-lg">
              <p className="text-sm text-blue-900">
                <strong>Auto-approved</strong> with {(merchant.approval_confidence || 0) * 100}% confidence.
                Full KYB documentation should be completed within 7 days.
              </p>
            </div>
          )}

          {/* Decision Radio */}
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-2">
              Decision
            </label>
            <div className="space-y-2">
              <label className="flex items-center gap-3 p-3 border-2 rounded-lg cursor-pointer hover:bg-slate-50 transition-colors"
                style={{ borderColor: decision === 'approved' ? 'rgb(34, 197, 94)' : 'rgb(226, 232, 240)' }}>
                <input
                  type="radio"
                  value="approved"
                  checked={decision === 'approved'}
                  onChange={(e) => setDecision(e.target.value as 'approved')}
                  className="w-4 h-4 text-green-600"
                />
                <CheckCircle className="w-5 h-5 text-green-600" />
                <span className="text-sm font-medium text-slate-900">Approve</span>
              </label>

              <label className="flex items-center gap-3 p-3 border-2 rounded-lg cursor-pointer hover:bg-slate-50 transition-colors"
                style={{ borderColor: decision === 'rejected' ? 'rgb(239, 68, 68)' : 'rgb(226, 232, 240)' }}>
                <input
                  type="radio"
                  value="rejected"
                  checked={decision === 'rejected'}
                  onChange={(e) => setDecision(e.target.value as 'rejected')}
                  className="w-4 h-4 text-red-600"
                />
                <XCircle className="w-5 h-5 text-red-600" />
                <span className="text-sm font-medium text-slate-900">Reject</span>
              </label>
            </div>
          </div>

          {/* Reason */}
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-2">
              Reason {decision === 'rejected' && '(required)'}
            </label>
            <textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder={decision === 'approved' ? 'Optional notes...' : 'Please provide a reason for rejection...'}
              rows={4}
              className="w-full px-3 py-2 text-sm border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent"
            />
          </div>
        </div>

        {/* Footer */}
        <div className="p-6 border-t border-slate-200 flex justify-end gap-3">
          <button
            onClick={onClose}
            disabled={submitting}
            className="px-4 py-2 text-sm font-medium text-slate-700 bg-white border border-slate-300 rounded-lg hover:bg-slate-50"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={submitting || (decision === 'rejected' && !reason)}
            className="px-4 py-2 text-sm font-medium text-white bg-primary rounded-lg hover:bg-primary/90 disabled:opacity-50"
          >
            {submitting ? 'Submitting...' : 'Submit Decision'}
          </button>
        </div>
      </div>
    </div>
  );
}

