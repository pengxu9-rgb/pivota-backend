'use client';

/**
 * [Phase 6] Merchant Commission Management Page
 */

import { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { DollarSign, Plus, Trash2 } from 'lucide-react';
import { apiClient } from '@/lib/api-client';

export default function CommissionPage() {
  const router = useRouter();
  const [offers, setOffers] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [offerMode, setOfferMode] = useState<'all' | 'premium'>('all');
  const [newOffer, setNewOffer] = useState({
    agent_type: '',
    offered_commission_rate: 2.0,
    min_order_amount: '' as string | number,  // Allow empty string for better UX
  });

  useEffect(() => {
    const token = localStorage.getItem('merchant_token');
    if (!token) {
      router.push('/login');
      return;
    }
    loadOffers();
  }, []);

  const loadOffers = async () => {
    setLoading(true);
    setError(null);
    try {
      const merchantId = localStorage.getItem('merchant_id');
      if (!merchantId) {
        setError('No merchant ID found');
        return;
      }
      
      const data = await apiClient.getCommissionOffers(merchantId);
      // Only show active offers (deleted ones are hidden)
      const activeOffers = (data.offers || []).filter((offer: any) => offer.is_active === true);
      setOffers(activeOffers);
    } catch (err: any) {
      console.error('Failed to load commission offers:', err);
      setError(err.response?.data?.detail || 'Failed to load offers');
    } finally {
      setLoading(false);
    }
  };

  const handleCreate = async () => {
    try {
      const merchantId = localStorage.getItem('merchant_id');
      if (!merchantId) return;
      
      // Check for duplicates
      const agentType = newOffer.agent_type || null;
      const rate = newOffer.offered_commission_rate / 100;
      const minAmount = typeof newOffer.min_order_amount === 'string' 
        ? (newOffer.min_order_amount === '' ? 0 : parseFloat(newOffer.min_order_amount))
        : newOffer.min_order_amount;
      
      // Check for duplicates
      const duplicate = offers.find(offer => 
        offer.agent_type === agentType &&
        Math.abs(offer.rate - rate) < 0.0001 &&
        Math.abs(offer.min_amount - minAmount) < 0.01
      );
      
      if (duplicate) {
        alert(`⚠️ Duplicate Offer!\n\nAn offer already exists with these settings:\n• Agent Type: ${agentType || 'All Agents'}\n• Rate: ${(rate * 100).toFixed(2)}%\n• Min Order: $${minAmount.toFixed(2)}\n\nPlease use different parameters or delete the existing offer first.`);
        return;
      }
      
      await apiClient.createCommissionOffer(merchantId, {
        agent_type: agentType,
        offered_commission_rate: rate,
        min_order_amount: minAmount,
      });
      
      setShowForm(false);
      setNewOffer({
        agent_type: '',
        offered_commission_rate: 2.0,
        min_order_amount: '',  // Reset to empty string
      });
      loadOffers();
    } catch (err: any) {
      alert('Failed to create offer: ' + (err.response?.data?.detail || err.message));
    }
  };

  const handleDelete = async (offerId: number) => {
    if (!confirm('🗑️ Delete this commission offer?\n\nThis action will remove the offer from your list.')) return;
    
    try {
      const merchantId = localStorage.getItem('merchant_id');
      if (!merchantId) return;
      
      // Delete via API (backend soft-deletes: is_active=false)
      await apiClient.deleteCommissionOffer(merchantId, offerId);
      
      // Immediately remove from UI list
      setOffers(offers.filter(offer => offer.id !== offerId));
      
      // Show success message
      const successMsg = document.createElement('div');
      successMsg.className = 'fixed top-4 right-4 bg-green-50 border border-green-200 text-green-700 px-6 py-3 rounded-lg shadow-lg z-50';
      successMsg.textContent = '✅ Offer deleted successfully';
      document.body.appendChild(successMsg);
      setTimeout(() => successMsg.remove(), 3000);
      
    } catch (err: any) {
      alert('❌ Failed to delete: ' + (err.response?.data?.detail || err.message));
      loadOffers();
    }
  };

  if (loading) {
    return (
      <div className="p-8 flex items-center justify-center">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-green-600"></div>
      </div>
    );
  }

  return (
    <div className="p-8">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold flex items-center gap-2">
            <DollarSign className="h-8 w-8 text-green-600" />
            Commission Management
          </h1>
          <p className="text-gray-600 mt-2">Set commission rates for agents</p>
        </div>
        <button
          onClick={() => setShowForm(true)}
          className="flex items-center gap-2 px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700"
        >
          <Plus className="h-5 w-5" />
          New Offer
        </button>
      </div>

      {error && (
        <div className="mb-4 p-4 bg-red-50 border border-red-200 rounded-lg text-red-700">
          {error}
        </div>
      )}

      {/* Platform Fallback Info */}
      <div className="mb-6 p-4 bg-blue-50 border border-blue-200 rounded-lg">
        <div className="flex items-start space-x-3">
          <svg className="w-6 h-6 text-blue-600 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <div className="flex-1">
            <h4 className="text-base font-semibold text-blue-900 mb-1">Platform Default Commission</h4>
            <p className="text-sm text-blue-700">
              When no commission offer is set for an order, the platform will automatically apply a <strong>1% fallback rate</strong> 
              to ensure agents are still compensated for their efforts. This applies to all order amounts.
            </p>
            <p className="text-sm text-blue-600 mt-2">
              💡 <em>Tip: Set competitive commission rates to attract more agents to promote your products!</em>
            </p>
          </div>
        </div>
      </div>

      {/* Offers List */}
      <div className="bg-white rounded-lg shadow">
        <table className="w-full">
          <thead className="bg-gray-50">
            <tr>
              <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Agent Type</th>
              <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Commission Rate</th>
              <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Min Order</th>
              <th className="px-6 py-3"></th>
            </tr>
          </thead>
          <tbody>
            {offers.length === 0 ? (
              <tr>
                <td colSpan={4} className="px-6 py-12 text-center text-gray-500">
                  No commission offers yet. Click "New Offer" to create one.
                </td>
              </tr>
            ) : (
              offers.map(offer => (
                <tr key={offer.id} className="hover:bg-gray-50">
                  <td className="px-6 py-4">
                    {offer.agent_type === 'premium' ? (
                      <span className="inline-flex items-center gap-1 px-3 py-1 bg-purple-100 text-purple-700 rounded-full text-sm font-medium">
                        ⭐ Premium
                      </span>
                    ) : (
                      <span className="font-medium text-gray-900">
                        All Agents
                      </span>
                    )}
                  </td>
                  <td className="px-6 py-4">
                    <span className="font-semibold text-green-600 text-lg">
                      {(offer.rate * 100).toFixed(2)}%
                    </span>
                  </td>
                  <td className="px-6 py-4">
                    <span className="text-gray-700">
                      {offer.min_amount > 0 ? `$${offer.min_amount.toFixed(2)}+` : 'No minimum'}
                    </span>
                  </td>
                  <td className="px-6 py-4 text-right">
                    <button 
                      onClick={() => handleDelete(offer.id)}
                      className="text-red-600 hover:bg-red-50 p-2 rounded-lg transition-colors"
                      title="Delete offer"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Create Form Modal */}
      {showForm && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 w-full max-w-md">
            <h2 className="text-xl font-bold mb-4">New Commission Offer</h2>
            <div className="space-y-4">
              {/* Offer Type Toggle */}
              <div>
                <label className="block text-sm font-medium mb-2">Offer Type</label>
                <div className="flex gap-3">
                  <button
                    type="button"
                    onClick={() => {
                      setOfferMode('all');
                      setNewOffer({...newOffer, agent_type: ''});
                    }}
                    className={`flex-1 px-4 py-3 rounded-lg border-2 transition-all ${
                      offerMode === 'all'
                        ? 'border-green-500 bg-green-50 text-green-700'
                        : 'border-gray-200 hover:border-gray-300'
                    }`}
                  >
                    <div className="font-medium">All Agents</div>
                    <div className="text-xs mt-1 opacity-75">Default rate for all tiers</div>
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setOfferMode('premium');
                      setNewOffer({...newOffer, agent_type: 'premium'});
                    }}
                    className={`flex-1 px-4 py-3 rounded-lg border-2 transition-all ${
                      offerMode === 'premium'
                        ? 'border-purple-500 bg-purple-50 text-purple-700'
                        : 'border-gray-200 hover:border-gray-300'
                    }`}
                  >
                    <div className="font-medium">⭐ Premium Only</div>
                    <div className="text-xs mt-1 opacity-75">Higher rate for premium agents</div>
                  </button>
                </div>
                <p className="text-xs text-gray-500 mt-2">
                  {offerMode === 'all' 
                    ? 'This rate applies to all agent tiers (basic and premium)' 
                    : 'This rate only applies to premium agents (higher tier)'}
                </p>
              </div>
              
              <div>
                <label className="block text-sm font-medium mb-2">Commission Rate (%)</label>
                <input
                  type="number"
                  min="0"
                  max="100"
                  step="0.1"
                  value={newOffer.offered_commission_rate}
                  onChange={e => setNewOffer({...newOffer, offered_commission_rate: parseFloat(e.target.value)})}
                  className="w-full px-3 py-2 border rounded-lg"
                />
              </div>
              
              <div>
                <label className="block text-sm font-medium mb-2">Minimum Order Amount ($)</label>
                <input
                  type="number"
                  min="0"
                  step="0.01"
                  placeholder="0.00"
                  value={newOffer.min_order_amount}
                  onChange={e => setNewOffer({
                    ...newOffer, 
                    min_order_amount: e.target.value === '' ? '' : parseFloat(e.target.value)
                  })}
                  className="w-full px-3 py-2 border rounded-lg"
                />
                <p className="text-xs text-gray-500 mt-1">Leave empty or 0 for no minimum</p>
              </div>
              
              <div className="flex gap-4">
                <button
                  onClick={() => setShowForm(false)}
                  className="flex-1 px-4 py-2 border rounded-lg hover:bg-gray-50"
                >
                  Cancel
                </button>
                <button
                  onClick={handleCreate}
                  className="flex-1 px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700"
                >
                  Create
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

