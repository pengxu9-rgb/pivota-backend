'use client';

import React from 'react';
import { 
  Clock, 
  CheckCircle, 
  XCircle, 
  AlertCircle, 
  DollarSign,
  User,
  Calendar,
  FileText,
  RefreshCw,
  Download
} from 'lucide-react';

interface RefundRecord {
  refund_id: string;
  amount: number;
  currency: string;
  reason: string;
  source: string;
  status: 'pending' | 'completed' | 'failed' | 'cancelled';
  status_type: 'success' | 'error' | 'warning' | 'info';
  status_message: string;
  created_by: string;
  created_at: string;
  created_at_formatted: string;
  processed_at?: string;
  processed_at_formatted?: string;
  processing_time_seconds?: number;
  error_message?: string;
  psp_refund_id?: string;
  metadata?: any;
}

interface RefundSummary {
  total_refunds: number;
  completed_amount: number;
  pending_amount: number;
  failed_count: number;
}

interface OrderSummary {
  order_id: string;
  total_amount: number;
  total_refunded: number;
  payment_status: string;
  currency: string;
  refundable_amount: number;
}

interface RefundHistoryTimelineProps {
  refunds: RefundRecord[];
  refundSummary: RefundSummary;
  orderSummary: OrderSummary;
  onRetryRefund?: (refundId: string) => void;
  onDownloadReceipt?: (refundId: string) => void;
}

export default function RefundHistoryTimeline({ 
  refunds, 
  refundSummary,
  orderSummary,
  onRetryRefund,
  onDownloadReceipt
}: RefundHistoryTimelineProps) {
  const getStatusIcon = (status: string, statusType: string) => {
    switch (status) {
      case 'completed':
        return <CheckCircle className="w-5 h-5 text-green-500" />;
      case 'failed':
        return <XCircle className="w-5 h-5 text-red-500" />;
      case 'pending':
        return <Clock className="w-5 h-5 text-yellow-500" />;
      default:
        return <AlertCircle className="w-5 h-5 text-gray-500" />;
    }
  };

  const formatCurrency = (amount: number, currency: string = 'USD') => {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: currency
    }).format(amount);
  };

  const formatDate = (dateString: string) => {
    if (!dateString) return 'N/A';
    const date = new Date(dateString);
    return new Intl.DateTimeFormat('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit'
    }).format(date);
  };

  const formatProcessingTime = (seconds?: number) => {
    if (!seconds) return null;
    if (seconds < 60) return `${Math.round(seconds)} seconds`;
    if (seconds < 3600) return `${Math.round(seconds / 60)} minutes`;
    return `${Math.round(seconds / 3600)} hours`;
  };

  if (!refunds || refunds.length === 0) {
    return (
      <div className="text-center py-8 text-gray-500">
        <RefreshCw className="w-12 h-12 mx-auto mb-3 text-gray-300" />
        <p>No refunds have been processed for this order yet.</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Summary Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
        <div className="bg-blue-50 rounded-lg p-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-blue-600 font-medium">Total Refunded</p>
              <p className="text-2xl font-bold text-blue-900">
                {formatCurrency(orderSummary.total_refunded, orderSummary.currency)}
              </p>
            </div>
            <DollarSign className="w-8 h-8 text-blue-400" />
          </div>
          <p className="text-xs text-blue-600 mt-2">
            of {formatCurrency(orderSummary.total_amount, orderSummary.currency)} total
          </p>
        </div>

        <div className="bg-green-50 rounded-lg p-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-green-600 font-medium">Refundable Amount</p>
              <p className="text-2xl font-bold text-green-900">
                {formatCurrency(orderSummary.refundable_amount, orderSummary.currency)}
              </p>
            </div>
            <CheckCircle className="w-8 h-8 text-green-400" />
          </div>
          <p className="text-xs text-green-600 mt-2">
            Available for refund
          </p>
        </div>

        <div className="bg-gray-50 rounded-lg p-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-gray-600 font-medium">Total Refunds</p>
              <p className="text-2xl font-bold text-gray-900">{refundSummary.total_refunds}</p>
            </div>
            <FileText className="w-8 h-8 text-gray-400" />
          </div>
          <p className="text-xs text-gray-600 mt-2">
            {refundSummary.failed_count > 0 && `${refundSummary.failed_count} failed`}
            {refundSummary.failed_count === 0 && 'All successful'}
          </p>
        </div>
      </div>

      {/* Timeline */}
      <div className="relative">
        <div className="absolute left-6 top-0 bottom-0 w-0.5 bg-gray-200"></div>
        
        {refunds.map((refund, index) => (
          <div key={refund.refund_id} className="relative flex items-start mb-8">
            {/* Timeline dot */}
            <div className="absolute left-4 w-4 h-4 bg-white border-2 border-gray-300 rounded-full"></div>
            
            {/* Status icon */}
            <div className="ml-12 mr-4">
              {getStatusIcon(refund.status, refund.status_type)}
            </div>
            
            {/* Content */}
            <div className="flex-1">
              <div className={`rounded-lg border p-4 ${
                refund.status === 'completed' ? 'bg-green-50 border-green-200' :
                refund.status === 'failed' ? 'bg-red-50 border-red-200' :
                refund.status === 'pending' ? 'bg-yellow-50 border-yellow-200' :
                'bg-gray-50 border-gray-200'
              }`}>
                <div className="flex justify-between items-start mb-2">
                  <div>
                    <h4 className="font-semibold text-gray-900">
                      {formatCurrency(refund.amount, refund.currency)} Refund
                    </h4>
                    <p className="text-sm text-gray-600 mt-1">{refund.status_message}</p>
                  </div>
                  <span className={`px-2 py-1 text-xs font-medium rounded-full ${
                    refund.status === 'completed' ? 'bg-green-100 text-green-800' :
                    refund.status === 'failed' ? 'bg-red-100 text-red-800' :
                    refund.status === 'pending' ? 'bg-yellow-100 text-yellow-800' :
                    'bg-gray-100 text-gray-800'
                  }`}>
                    {refund.status.toUpperCase()}
                  </span>
                </div>
                
                <div className="grid grid-cols-2 gap-4 text-sm mt-3">
                  <div className="flex items-center text-gray-600">
                    <FileText className="w-4 h-4 mr-2" />
                    <span>Reason: {refund.reason}</span>
                  </div>
                  <div className="flex items-center text-gray-600">
                    <User className="w-4 h-4 mr-2" />
                    <span>By: {refund.created_by || 'System'}</span>
                  </div>
                  <div className="flex items-center text-gray-600">
                    <Calendar className="w-4 h-4 mr-2" />
                    <span>Initiated: {formatDate(refund.created_at_formatted)}</span>
                  </div>
                  {refund.processed_at_formatted && (
                    <div className="flex items-center text-gray-600">
                      <Clock className="w-4 h-4 mr-2" />
                      <span>
                        Processed: {formatDate(refund.processed_at_formatted)}
                        {refund.processing_time_seconds && (
                          <span className="text-xs ml-1">
                            ({formatProcessingTime(refund.processing_time_seconds)})
                          </span>
                        )}
                      </span>
                    </div>
                  )}
                </div>
                
                {/* PSP Reference */}
                {refund.psp_refund_id && (
                  <div className="mt-3 pt-3 border-t border-gray-200">
                    <p className="text-xs text-gray-500">
                      PSP Reference: {refund.psp_refund_id}
                    </p>
                  </div>
                )}
                
                {/* Error message for failed refunds */}
                {refund.status === 'failed' && refund.error_message && (
                  <div className="mt-3 p-2 bg-red-100 rounded text-sm text-red-700">
                    <strong>Error:</strong> {refund.error_message}
                  </div>
                )}
                
                {/* Actions */}
                <div className="flex gap-2 mt-4">
                  {refund.status === 'failed' && onRetryRefund && (
                    <button
                      onClick={() => onRetryRefund(refund.refund_id)}
                      className="flex items-center gap-1 px-3 py-1 text-sm bg-orange-500 text-white rounded hover:bg-orange-600"
                    >
                      <RefreshCw className="w-3 h-3" />
                      Retry
                    </button>
                  )}
                  {refund.status === 'completed' && onDownloadReceipt && (
                    <button
                      onClick={() => onDownloadReceipt(refund.refund_id)}
                      className="flex items-center gap-1 px-3 py-1 text-sm bg-gray-500 text-white rounded hover:bg-gray-600"
                    >
                      <Download className="w-3 h-3" />
                      Receipt
                    </button>
                  )}
                </div>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
