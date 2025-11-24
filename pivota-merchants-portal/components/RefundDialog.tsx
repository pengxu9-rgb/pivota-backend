'use client';

import { useState } from 'react';
import { X } from 'lucide-react';

interface RefundDialogProps {
  order: any;
  onClose: () => void;
  onRefund: (amount: number, reason: string) => Promise<void>;
}

const REFUND_REASONS = {
  'customer_request': 'Customer Request',
  'defective_product': 'Defective Product',
  'wrong_item': 'Wrong Item Sent',
  'not_as_described': 'Not as Described',
  'damaged_in_shipping': 'Damaged in Shipping',
  'duplicate_order': 'Duplicate Order',
  'other': 'Other'
};

export default function RefundDialog({ order, onClose, onRefund }: RefundDialogProps) {
  const [amount, setAmount] = useState<string>('');
  const [reason, setReason] = useState<string>('customer_request');
  const [isProcessing, setIsProcessing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Calculate refundable amount
  const orderTotal = parseFloat(order.total || '0');
  const totalRefunded = parseFloat(order.total_refunded || '0');
  const maxRefundable = orderTotal - totalRefunded;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    
    const refundAmount = parseFloat(amount);
    
    // Validation
    if (isNaN(refundAmount) || refundAmount <= 0) {
      setError('Please enter a valid amount');
      return;
    }
    
    if (refundAmount > maxRefundable) {
      setError(`Amount exceeds refundable balance of $${maxRefundable.toFixed(2)}`);
      return;
    }
    
    if (!reason) {
      setError('Please select a refund reason');
      return;
    }

    setError(null);
    setIsProcessing(true);

    try {
      await onRefund(refundAmount, reason);
      onClose();
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Failed to process refund');
    } finally {
      setIsProcessing(false);
    }
  };

  const setFullRefund = () => {
    setAmount(maxRefundable.toFixed(2));
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
      <div className="bg-white rounded-lg shadow-xl max-w-md w-full p-6">
        {/* Header */}
        <div className="flex justify-between items-center mb-4">
          <h2 className="text-xl font-bold">Process Refund</h2>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600"
          >
            <X size={24} />
          </button>
        </div>

        {/* Order Info */}
        <div className="bg-gray-50 p-3 rounded mb-4 text-sm">
          <div className="flex justify-between mb-1">
            <span className="text-gray-600">Order Total:</span>
            <span className="font-medium">${orderTotal.toFixed(2)}</span>
          </div>
          <div className="flex justify-between mb-1">
            <span className="text-gray-600">Already Refunded:</span>
            <span className="font-medium text-red-600">${totalRefunded.toFixed(2)}</span>
          </div>
          <div className="flex justify-between pt-2 border-t border-gray-200">
            <span className="text-gray-900 font-semibold">Refundable Amount:</span>
            <span className="font-bold text-green-600">${maxRefundable.toFixed(2)}</span>
          </div>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit}>
          {/* Amount Input */}
          <div className="mb-4">
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Refund Amount *
            </label>
            <div className="flex gap-2">
              <div className="relative flex-1">
                <span className="absolute left-3 top-2.5 text-gray-500">$</span>
                <input
                  type="number"
                  step="0.01"
                  min="0.01"
                  max={maxRefundable}
                  value={amount}
                  onChange={(e) => setAmount(e.target.value)}
                  className="w-full pl-7 pr-3 py-2 border border-gray-300 rounded-md focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                  placeholder="0.00"
                  required
                  disabled={isProcessing}
                />
              </div>
              <button
                type="button"
                onClick={setFullRefund}
                className="px-3 py-2 text-sm bg-gray-100 hover:bg-gray-200 rounded-md whitespace-nowrap"
                disabled={isProcessing}
              >
                Full Refund
              </button>
            </div>
          </div>

          {/* Reason Select */}
          <div className="mb-4">
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Reason *
            </label>
            <select
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-md focus:ring-2 focus:ring-blue-500 focus:border-transparent"
              required
              disabled={isProcessing}
            >
              {Object.entries(REFUND_REASONS).map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </div>

          {/* Error Message */}
          {error && (
            <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-md">
              <p className="text-sm text-red-800">{error}</p>
            </div>
          )}

          {/* Info Message */}
          <div className="mb-4 p-3 bg-blue-50 border border-blue-200 rounded-md">
            <p className="text-xs text-blue-800">
              <strong>Note:</strong> Refunds will be processed through {order.psp_used || 'Stripe'} 
              and typically appear in 3-5 business days.
            </p>
          </div>

          {/* Actions */}
          <div className="flex gap-3">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 px-4 py-2 border border-gray-300 rounded-md text-gray-700 hover:bg-gray-50"
              disabled={isProcessing}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="flex-1 px-4 py-2 bg-red-500 text-white rounded-md hover:bg-red-600 disabled:opacity-50 disabled:cursor-not-allowed"
              disabled={isProcessing}
            >
              {isProcessing ? 'Processing...' : 'Process Refund'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

