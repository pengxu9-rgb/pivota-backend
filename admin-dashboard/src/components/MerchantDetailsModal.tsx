import { Merchant } from '../lib/api';
import { X } from 'lucide-react';
import { formatDateTime } from '../lib/utils';

interface MerchantDetailsModalProps {
  merchant: Merchant;
  open: boolean;
  onClose: () => void;
}

export default function MerchantDetailsModal({ merchant, open, onClose }: MerchantDetailsModalProps) {
  if (!open) return null;

  const details = [
    { label: 'Merchant ID', value: merchant.merchant_id },
    { label: 'Business Name', value: merchant.business_name },
    { label: 'Store URL', value: merchant.store_url },
    { label: 'Website', value: merchant.website || '-' },
    { label: 'Region', value: merchant.region },
    { label: 'Contact Email', value: merchant.contact_email },
    { label: 'Contact Phone', value: merchant.contact_phone || '-' },
    { label: 'Status', value: merchant.status },
    { label: 'Auto Approved', value: merchant.auto_approved ? 'Yes' : 'No' },
    { label: 'Approval Confidence', value: merchant.approval_confidence ? `${(merchant.approval_confidence * 100).toFixed(0)}%` : '-' },
    { label: 'PSP Connected', value: merchant.psp_connected ? 'Yes' : 'No' },
    { label: 'PSP Type', value: merchant.psp_type || '-' },
    { label: 'API Key', value: merchant.api_key || '-' },
    { label: 'Full KYB Deadline', value: merchant.full_kyb_deadline ? formatDateTime(merchant.full_kyb_deadline) : '-' },
    { label: 'Created At', value: formatDateTime(merchant.created_at) },
    { label: 'Updated At', value: merchant.updated_at ? formatDateTime(merchant.updated_at) : '-' },
  ];

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div
        className="bg-white rounded-xl shadow-2xl max-w-2xl w-full max-h-[90vh] overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between p-6 border-b border-slate-200">
          <div>
            <h2 className="text-xl font-bold text-slate-900">Merchant Details</h2>
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
        <div className="p-6 overflow-y-auto max-h-[calc(90vh-140px)]">
          <div className="space-y-4">
            {details.map((detail) => (
              <div key={detail.label} className="flex justify-between py-2 border-b border-slate-100">
                <span className="text-sm font-medium text-slate-600">{detail.label}</span>
                <span className="text-sm text-slate-900 text-right max-w-md truncate">
                  {detail.value}
                </span>
              </div>
            ))}
          </div>

          {/* KYC Documents */}
          {merchant.kyc_documents && merchant.kyc_documents.length > 0 && (
            <div className="mt-6">
              <h3 className="text-sm font-semibold text-slate-900 mb-3">KYC Documents</h3>
              <div className="space-y-2">
                {merchant.kyc_documents.map((doc: any, idx: number) => (
                  <div
                    key={idx}
                    className="p-3 bg-slate-50 rounded-lg flex items-center justify-between"
                  >
                    <div>
                      <p className="text-sm font-medium text-slate-900">
                        {doc.filename || `Document ${idx + 1}`}
                      </p>
                      <p className="text-xs text-slate-500">{doc.type || 'file'}</p>
                    </div>
                    {doc.url && (
                      <a
                        href={doc.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-xs text-primary hover:underline"
                      >
                        Open
                      </a>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="p-6 border-t border-slate-200 flex justify-end">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm font-medium text-white bg-primary rounded-lg hover:bg-primary/90"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

