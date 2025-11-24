'use client';

import { useState, useEffect } from 'react';
import { ShoppingBag, Download, Search, Filter, Truck, DollarSign } from 'lucide-react';
import { apiClient } from '@/lib/api-client';
import RefundDialog from '@/components/RefundDialog';

export default function OrdersPage() {
  const [orders, setOrders] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchTerm, setSearchTerm] = useState('');
  const [statusFilter, setStatusFilter] = useState('all');
  const [selectedOrder, setSelectedOrder] = useState<any>(null);
  const [showOrderModal, setShowOrderModal] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [currentPage, setCurrentPage] = useState(1);
  const [totalOrders, setTotalOrders] = useState(0);
  const [itemsPerPage] = useState(50);
  
  // Shipping state
  const [showShippingForm, setShowShippingForm] = useState(false);
  const [shippingData, setShippingData] = useState({ tracking_number: '', carrier: '' });
  const [shippingLoading, setShippingLoading] = useState(false);
  
  // Refund state
  const [showRefundDialog, setShowRefundDialog] = useState(false);
  const [refundHistory, setRefundHistory] = useState<any[]>([]);

  useEffect(() => {
    loadOrders();
  }, [currentPage, statusFilter]);

  const loadOrders = async () => {
    try {
      setLoading(true);
      const offset = (currentPage - 1) * itemsPerPage;
      const params: any = { limit: itemsPerPage, offset };
      if (statusFilter !== 'all') {
        params.status = statusFilter;
      }
      const data = await apiClient.getOrders(params);
      setOrders(data.orders || []);
      setTotalOrders(data.total || 0);
    } catch (error) {
      console.error('Failed to load orders:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleViewOrder = async (orderId: string) => {
    try {
      const orderDetails = await apiClient.getOrderDetails(orderId);
      await openOrderModal(orderDetails);
    } catch (error) {
      console.error('Failed to load order:', error);
      alert('Failed to load order details');
    }
  };

  const handleExport = async () => {
    try {
      setExporting(true);
      const result = await apiClient.exportOrders();
      alert(result.message || 'Export started. You will receive an email when ready.');
    } catch (error: any) {
      console.error('Export failed:', error);
      alert(error.response?.data?.detail || 'Failed to export orders');
    } finally {
      setExporting(false);
    }
  };
  
  const handleMarkShipped = async () => {
    if (!shippingData.tracking_number || !shippingData.carrier) {
      alert('Please enter tracking number and carrier');
      return;
    }
    
    setShippingLoading(true);
    try {
      const orderId = selectedOrder.data?.order_id || selectedOrder.order_id;
      await apiClient.markOrderShipped(orderId, shippingData.tracking_number, shippingData.carrier);
      alert('Order marked as shipped!');
      setShowShippingForm(false);
      setShowOrderModal(false);
      loadOrders(); // Refresh list
    } catch (error: any) {
      console.error('Failed to mark shipped:', error);
      alert(error.response?.data?.detail || 'Failed to mark order as shipped');
    } finally {
      setShippingLoading(false);
    }
  };
  
  const loadRefundHistory = async (orderId: string) => {
    try {
      const result = await apiClient.getOrderRefunds(orderId);
      setRefundHistory(result.refunds || []);
    } catch (error: any) {
      console.error('Failed to load refund history:', error);
      setRefundHistory([]);
    }
  };
  
  const handleRefund = async (amount: number, reason: string) => {
    try {
      const orderId = selectedOrder.data?.order_id || selectedOrder.order_id;
      await apiClient.refundOrderV2(orderId, amount, reason);
      alert('Refund processed successfully!');
      setShowRefundDialog(false);
      loadRefundHistory(orderId); // Reload refund history
      loadOrders(); // Refresh order list
    } catch (error: any) {
      console.error('Failed to process refund:', error);
      throw error; // Let dialog handle the error
    }
  };
  
  const openOrderModal = async (order: any) => {
    setSelectedOrder(order);
    setShowOrderModal(true);
    setShowShippingForm(false);
    
    // Load refund history
    const orderId = order.data?.order_id || order.order_id;
    if (orderId) {
      await loadRefundHistory(orderId);
    }
  };

  const formatCurrency = (amount: number) => {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
    }).format(amount);
  };

  const filteredOrders = orders.filter(order => {
    const matchesSearch = searchTerm === '' || 
      order.order_number?.toLowerCase().includes(searchTerm.toLowerCase()) ||
      order.customer_email?.toLowerCase().includes(searchTerm.toLowerCase());
    const matchesStatus = statusFilter === 'all' || order.status === statusFilter;
    return matchesSearch && matchesStatus;
  });

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600"></div>
      </div>
    );
  }
  
  // Helper to safely get order data
  const getOrderData = () => {
    if (!selectedOrder) return null;
    return selectedOrder.data || selectedOrder;
  };
  
  const orderData = getOrderData();
  const normalizeAddress = (address: any) => {
    if (!address) return null;
    if (typeof address === 'object') return address;
    if (typeof address === 'string') {
      try {
        const parsed = JSON.parse(address);
        return typeof parsed === 'object' ? parsed : null;
      } catch (err) {
        console.warn('Failed to parse shipping address', err);
        return null;
      }
    }
    return null;
  };
  const normalizeItems = (items: any): any[] => {
    if (!items) return [];
    if (Array.isArray(items)) return items;
    try {
      const parsed = JSON.parse(items);
      return Array.isArray(parsed) ? parsed : [];
    } catch (err) {
      console.warn('Failed to parse order items', err);
      return [];
    }
  };
  const orderItems = normalizeItems(orderData?.items);
  const shippingAddress = normalizeAddress(orderData?.shipping_address);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Orders</h1>
          <p className="text-gray-600">Manage and track all your orders</p>
        </div>
        <button 
          onClick={handleExport}
          disabled={exporting}
          className="flex items-center space-x-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50"
        >
          <Download className="w-4 h-4" />
          <span>{exporting ? 'Exporting...' : 'Export'}</span>
        </button>
      </div>

      <div className="bg-white rounded-lg shadow">
        <div className="p-4 border-b">
          <div className="flex items-center space-x-4">
            <div className="flex-1 relative">
              <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-gray-400" />
              <input
                type="text"
                placeholder="Search orders..."
                value={searchTerm}
                onChange={(e) => setSearchTerm(e.target.value)}
                className="w-full pl-10 pr-4 py-2 border rounded-lg"
              />
            </div>
            <select
              value={statusFilter}
              onChange={(e) => {
                setStatusFilter(e.target.value);
                setCurrentPage(1); // Reset to first page when filter changes
              }}
              className="px-4 py-2 border rounded-lg"
            >
              <option value="all">All Status</option>
              <option value="pending">Pending</option>
              <option value="processing">Processing</option>
              <option value="completed">Completed</option>
              <option value="cancelled">Cancelled</option>
            </select>
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Order ID</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Customer</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Amount</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Date</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200">
              {filteredOrders.length > 0 ? (
                filteredOrders.map((order) => (
                  <tr key={order.id || order.order_id} className="hover:bg-gray-50">
                    <td className="px-6 py-4 whitespace-nowrap text-sm font-medium text-gray-900">
                      <span 
                        className="font-mono cursor-help" 
                        title={order.order_id || order.order_number || order.id}
                      >
                        {(order.order_id || order.order_number || order.id).substring(0, 12)}...
                      </span>
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-600">
                      {order.customer?.email || order.customer_email || 'Guest'}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-600">
                      {formatCurrency(order.amount || order.total_amount || 0)}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap">
                      <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                        order.status === 'completed' ? 'bg-green-100 text-green-800' :
                        order.status === 'processing' ? 'bg-blue-100 text-blue-800' :
                        order.status === 'pending' ? 'bg-yellow-100 text-yellow-800' :
                        'bg-gray-100 text-gray-800'
                      }`}>
                        {order.status || 'pending'}
                      </span>
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                      {new Date(order.created_at).toLocaleDateString()}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm">
                      <button 
                        onClick={() => handleViewOrder(order.order_id || order.id)}
                        className="text-blue-600 hover:text-blue-800"
                      >
                        View
                      </button>
                    </td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={6} className="px-6 py-12 text-center text-gray-500">
                    <ShoppingBag className="w-12 h-12 text-gray-400 mx-auto mb-3" />
                    <p>No orders found</p>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {/* Pagination */}
        {totalOrders > 0 && (
          <div className="px-6 py-4 border-t flex items-center justify-between">
            <div className="text-sm text-gray-700">
              Showing <span className="font-medium">{((currentPage - 1) * itemsPerPage) + 1}</span> to{' '}
              <span className="font-medium">
                {Math.min(currentPage * itemsPerPage, totalOrders)}
              </span>{' '}
              of <span className="font-medium">{totalOrders}</span> orders
            </div>
            <div className="flex items-center space-x-2">
              <button
                onClick={() => setCurrentPage(p => Math.max(1, p - 1))}
                disabled={currentPage === 1}
                className="px-3 py-1 border rounded hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Previous
              </button>
              <span className="text-sm text-gray-700">
                Page {currentPage} of {Math.ceil(totalOrders / itemsPerPage)}
              </span>
              <button
                onClick={() => setCurrentPage(p => Math.min(Math.ceil(totalOrders / itemsPerPage), p + 1))}
                disabled={currentPage >= Math.ceil(totalOrders / itemsPerPage)}
                className="px-3 py-1 border rounded hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Next
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Order Details Modal */}
      {showOrderModal && orderData && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-lg max-w-2xl w-full max-h-[90vh] overflow-y-auto">
            <div className="p-6">
              <div className="flex justify-between items-center mb-4">
                <h2 className="text-2xl font-bold">Order Details</h2>
                <button
                  onClick={() => setShowOrderModal(false)}
                  className="text-gray-400 hover:text-gray-600"
                >
                  ✕
                </button>
              </div>

              <div className="space-y-4">
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <p className="text-sm text-gray-600">Order ID</p>
                    <p className="font-semibold">{orderData.order_id}</p>
                  </div>
                  <div>
                    <p className="text-sm text-gray-600">Status</p>
                    <p className="font-semibold capitalize">{orderData.status}</p>
                  </div>
                  <div>
                    <p className="text-sm text-gray-600">Payment Status</p>
                    <p className="font-semibold capitalize">{orderData.payment_status}</p>
                  </div>
                  <div>
                    <p className="text-sm text-gray-600">Total Amount</p>
                    <p className="font-semibold">
                      ${(orderData.total || 0).toFixed(2)}
                    </p>
                  </div>
                </div>

                <div className="border-t pt-4">
                  <h3 className="font-semibold mb-2">Customer</h3>
                  <p className="text-sm">
                    <span className="text-gray-600">Name:</span>{' '}
                    {orderData.customer?.name || orderData.customer_name || shippingAddress?.name || 'N/A'}
                  </p>
                  <p className="text-sm">
                    <span className="text-gray-600">Email:</span>{' '}
                    {orderData.customer?.email || orderData.customer_email || 'N/A'}
                  </p>
                  {shippingAddress && (
                    <p className="text-sm mt-1 text-gray-500">
                      {shippingAddress.address_line1}
                      {shippingAddress.address_line2 ? `, ${shippingAddress.address_line2}` : ''}, {shippingAddress.city}, {shippingAddress.state} {shippingAddress.postal_code}
                    </p>
                  )}
                </div>

                {orderItems.length > 0 && (
                  <div className="border-t pt-4">
                    <h3 className="font-semibold mb-2">Items</h3>
                    {orderItems.map((item: any, idx: number) => (
                      <div key={idx} className="text-sm py-2 border-b last:border-0">
                        <p className="font-medium">{item.name || item.product_name || item.product_title}</p>
                        <p className="text-gray-600">
                          Quantity: {item.quantity} × ${item.price || item.unit_price}
                        </p>
                      </div>
                    ))}
                  </div>
                )}

                <div className="border-t pt-4">
                  <p className="text-sm">
                    <span className="text-gray-600">Created:</span>{' '}
                    {new Date(orderData.created_at).toLocaleString()}
                  </p>
                  {orderData.fulfillment_status === 'shipped' && (
                    <p className="text-sm text-green-600 mt-1">
                      Shipped via {orderData.carrier || 'Unknown'} (Track: {orderData.tracking_number || 'N/A'})
                    </p>
                  )}
                </div>
                
                {/* Shipping Form for Agent Orders */}
                {orderData.status !== 'completed' && orderData.status !== 'cancelled' && !orderData.store_id && (
                  <div className="border-t pt-4">
                    {!showShippingForm ? (
                      <button
                        onClick={() => setShowShippingForm(true)}
                        className="flex items-center space-x-2 text-blue-600 hover:text-blue-800 font-medium text-sm"
                      >
                        <Truck className="w-4 h-4" />
                        <span>Mark as Shipped</span>
                      </button>
                    ) : (
                      <div className="bg-gray-50 p-4 rounded-lg space-y-3">
                        <h4 className="font-medium text-sm">Fulfillment Details</h4>
                        <div className="grid grid-cols-2 gap-3">
                          <div>
                            <label className="block text-xs text-gray-600 mb-1">Tracking Number</label>
                            <input
                              type="text"
                              value={shippingData.tracking_number}
                              onChange={(e) => setShippingData({...shippingData, tracking_number: e.target.value})}
                              className="w-full border rounded px-2 py-1 text-sm"
                              placeholder="e.g. 1Z999..."
                            />
                          </div>
                          <div>
                            <label className="block text-xs text-gray-600 mb-1">Carrier</label>
                            <select
                              value={shippingData.carrier}
                              onChange={(e) => setShippingData({...shippingData, carrier: e.target.value})}
                              className="w-full border rounded px-2 py-1 text-sm"
                            >
                              <option value="">Select Carrier</option>
                              <option value="USPS">USPS</option>
                              <option value="UPS">UPS</option>
                              <option value="FedEx">FedEx</option>
                              <option value="DHL">DHL</option>
                            </select>
                          </div>
                        </div>
                        <div className="flex justify-end space-x-2 pt-2">
                          <button
                            onClick={() => setShowShippingForm(false)}
                            className="px-3 py-1 text-xs border rounded hover:bg-gray-100"
                          >
                            Cancel
                          </button>
                          <button
                            onClick={handleMarkShipped}
                            disabled={shippingLoading || !shippingData.tracking_number || !shippingData.carrier}
                            className="px-3 py-1 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
                          >
                            {shippingLoading ? 'Saving...' : 'Confirm Shipment'}
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
              
              {/* Refund Section */}
              {orderData.payment_status === 'paid' || orderData.payment_status === 'partially_refunded' ? (
                <div className="border-t pt-4 mt-4">
                  <div className="flex justify-between items-center mb-3">
                    <h3 className="font-semibold">Refunds</h3>
                    {(orderData.total - (orderData.total_refunded || 0)) > 0 && (
                      <button
                        onClick={() => setShowRefundDialog(true)}
                        className="flex items-center space-x-2 text-red-600 hover:text-red-800 font-medium text-sm"
                      >
                        <DollarSign className="w-4 h-4" />
                        <span>Process Refund</span>
                      </button>
                    )}
                  </div>
                  
                  {/* Refund Summary */}
                  <div className="bg-gray-50 p-3 rounded mb-3 text-sm">
                    <div className="flex justify-between">
                      <span className="text-gray-600">Refundable:</span>
                      <span className="font-medium text-green-600">
                        ${((orderData.total || 0) - (orderData.total_refunded || 0)).toFixed(2)}
                      </span>
                    </div>
                    {orderData.total_refunded > 0 && (
                      <div className="flex justify-between mt-1">
                        <span className="text-gray-600">Already Refunded:</span>
                        <span className="font-medium text-red-600">
                          ${(orderData.total_refunded || 0).toFixed(2)}
                        </span>
                      </div>
                    )}
                  </div>
                  
                  {/* Refund History */}
                  {refundHistory.length > 0 && (
                    <div className="space-y-2">
                      <h4 className="text-sm font-medium text-gray-700">Refund History</h4>
                      {refundHistory.map((refund: any, idx: number) => (
                        <div key={idx} className="bg-white border rounded p-2 text-sm">
                          <div className="flex justify-between">
                            <span className="font-medium">${parseFloat(refund.amount).toFixed(2)}</span>
                            <span className={`text-xs px-2 py-0.5 rounded ${
                              refund.status === 'completed' ? 'bg-green-100 text-green-800' :
                              refund.status === 'pending' ? 'bg-yellow-100 text-yellow-800' :
                              'bg-red-100 text-red-800'
                            }`}>
                              {refund.status}
                            </span>
                          </div>
                          <div className="text-xs text-gray-500 mt-1">
                            {refund.reason && <span>Reason: {refund.reason}</span>}
                            <span className="ml-2">
                              {new Date(refund.created_at).toLocaleString()}
                            </span>
                          </div>
                          {refund.error_message && (
                            <div className="text-xs text-red-600 mt-1">
                              Error: {refund.error_message}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ) : null}

              <div className="mt-6 flex justify-end">
                <button
                  onClick={() => setShowOrderModal(false)}
                  className="px-4 py-2 bg-gray-200 text-gray-700 rounded hover:bg-gray-300"
                >
                  Close
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
      
      {/* Refund Dialog */}
      {showRefundDialog && selectedOrder && (
        <RefundDialog
          order={selectedOrder.data || selectedOrder}
          onClose={() => setShowRefundDialog(false)}
          onRefund={handleRefund}
        />
      )}
    </div>
  );
}
