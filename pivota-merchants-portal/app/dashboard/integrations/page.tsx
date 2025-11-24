'use client';

import { useState, useEffect } from 'react';
import {
  Store,
  CreditCard,
  Plus,
  Settings,
  Check,
  X,
  Loader2,
  RefreshCw,
  Link as LinkIcon,
  AlertCircle,
  Copy,
  Key,
  Webhook,
} from 'lucide-react';
import { apiClient } from '@/lib/api-client';
import PSPRoutingConfig from '@/components/PSPRoutingConfig';
import ConnectStoreModal from '@/components/ConnectStoreModal';
import { PSPConfigForm } from '@/components/PSPConfigForm';

export default function IntegrationsPage() {
  const [loading, setLoading] = useState(true);
  const [merchantId, setMerchantId] = useState<string>('');
  const [activeTab, setActiveTab] = useState<'stores' | 'psps' | 'routing' | 'webhooks'>('stores');
  
  // Data states
  const [connectedStores, setConnectedStores] = useState<any[]>([]);
  const [connectedPSPs, setConnectedPSPs] = useState<any[]>([]);
  const [webhookConfig, setWebhookConfig] = useState<any>(null);
  const [apiKey, setApiKey] = useState<string>('');
  
  // UI states
  const [showConnectStore, setShowConnectStore] = useState(false);
  const [showConnectPSP, setShowConnectPSP] = useState(false);
  const [selectedPSPProvider, setSelectedPSPProvider] = useState<string>('');
  const [syncing, setSyncing] = useState(false);
  const [syncingStoreId, setSyncingStoreId] = useState<string | null>(null);
  const [testing, setTesting] = useState<string | null>(null);

  useEffect(() => {
    const id = localStorage.getItem('merchant_id') || '';
    const key = localStorage.getItem('merchant_api_key') || 'pk_live_' + Math.random().toString(36).substring(2, 15);
    setMerchantId(id);
    setApiKey(key);
    loadIntegrationData(id);
  }, []);

  const loadIntegrationData = async (merchantId: string) => {
    try {
      setLoading(true);
      
      const [stores, psps, webhook] = await Promise.all([
        merchantId ? apiClient.getConnectedStores(merchantId).catch(() => []) : [],
        merchantId ? apiClient.getPSPs(merchantId).catch(() => []) : [],
        apiClient.getWebhookConfig().catch(() => null),
      ]);
      
      setConnectedStores(stores);
      setConnectedPSPs(psps);
      setWebhookConfig(webhook);
      
      console.log('📦 Integration data loaded:', { stores, psps, webhook });
    } catch (error) {
      console.error('❌ Failed to load integration data:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleStoreConnected = async () => {
    await loadIntegrationData(merchantId);
  };

  const handlePSPConnected = async () => {
    await loadIntegrationData(merchantId);
  };

  const handleSyncProducts = async (store: any) => {
    setSyncingStoreId(store.id);
    try {
      let result;
      if (store.platform === 'shopify') {
        result = await apiClient.syncShopifyProducts();
      } else if (store.platform === 'wix') {
        result = await apiClient.syncPlatformProducts('wix');
      } else {
        result = await apiClient.syncPlatformProducts(store.platform);
      }
      alert(result.message || `✅ ${store.platform} products synced successfully!`);
      await loadIntegrationData(merchantId); // Reload to update product counts
    } catch (error: any) {
      alert('❌ Failed to sync products: ' + (error.response?.data?.detail || error.message));
    } finally {
      setSyncingStoreId(null);
    }
  };

  const handleDeleteStore = async (store: any) => {
    if (!confirm(`Are you sure you want to delete the store "${store.store_name || store.name}"?\n\nThis will disconnect the store.`)) {
      return;
    }
    
    try {
      setLoading(true);
      const response = await apiClient.client.delete(`/merchant/integrations/store/${store.id}`);
      alert(response.data.message || '✅ Store deleted');
      await loadIntegrationData(merchantId);
    } catch (error: any) {
      alert('❌ Delete failed: ' + (error.response?.data?.detail || error.message));
    } finally {
      setLoading(false);
    }
  };

  const handleDeletePSP = async (psp: any) => {
    if (!confirm(`Are you sure you want to delete PSP "${psp.name}"?\n\nThis will disconnect the payment processor.`)) {
      return;
    }
    
    try {
      setLoading(true);
      const response = await apiClient.client.delete(`/merchant/integrations/psp/${psp.id}`);
      alert(response.data.message || '✅ PSP deleted');
      await loadIntegrationData(merchantId);
    } catch (error: any) {
      alert('❌ Delete failed: ' + (error.response?.data?.detail || error.message));
    } finally {
      setLoading(false);
    }
  };

  const handleTestPSP = async (pspId: string) => {
    setTesting(pspId);
    try {
      await apiClient.testPSP(pspId);
      alert('✅ PSP connection test successful!');
    } catch (error: any) {
      alert('❌ PSP test failed: ' + (error.response?.data?.detail || error.message));
    } finally {
      setTesting(null);
    }
  };

  const handleSaveWebhook = async () => {
    const url = prompt('Enter your webhook URL:', webhookConfig?.url || '');
    if (!url) return;
    
    try {
      await apiClient.updateWebhookConfig({
        url,
        events: ['order.created', 'payment.completed', 'payment.failed'],
        enabled: true,
      });
      alert('✅ Webhook configuration saved!');
      await loadIntegrationData(merchantId);
    } catch (error: any) {
      alert('❌ Failed to save webhook: ' + (error.response?.data?.detail || error.message));
    }
  };

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text);
    alert('Copied to clipboard!');
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Integrations</h1>
        <p className="text-gray-600">Connect your stores, payment processors, and configure webhooks</p>
      </div>


      {/* Tabs */}
      <div className="border-b border-gray-200">
        <nav className="-mb-px flex space-x-8">
          <button
            onClick={() => setActiveTab('stores')}
            className={`py-2 px-1 border-b-2 font-medium text-sm ${
              activeTab === 'stores'
                ? 'border-blue-500 text-blue-600'
                : 'border-transparent text-gray-500 hover:text-gray-700'
            }`}
          >
            <Store className="w-4 h-4 inline mr-2" />
            Stores ({connectedStores.length})
          </button>
          <button
            onClick={() => setActiveTab('psps')}
            className={`py-2 px-1 border-b-2 font-medium text-sm ${
              activeTab === 'psps'
                ? 'border-blue-500 text-blue-600'
                : 'border-transparent text-gray-500 hover:text-gray-700'
            }`}
          >
            <CreditCard className="w-4 h-4 inline mr-2" />
            Payment Processors ({connectedPSPs.filter(p => p.is_active).length})
          </button>
          {connectedPSPs.filter(p => p.is_active).length > 1 && (
            <button
              onClick={() => setActiveTab('routing')}
              className={`py-2 px-1 border-b-2 font-medium text-sm ${
                activeTab === 'routing'
                  ? 'border-blue-500 text-blue-600'
                  : 'border-transparent text-gray-500 hover:text-gray-700'
              }`}
            >
              <Settings className="w-4 h-4 inline mr-2" />
              Routing
            </button>
          )}
          <button
            onClick={() => setActiveTab('webhooks')}
            className={`py-2 px-1 border-b-2 font-medium text-sm ${
              activeTab === 'webhooks'
                ? 'border-blue-500 text-blue-600'
                : 'border-transparent text-gray-500 hover:text-gray-700'
            }`}
          >
            <Webhook className="w-4 h-4 inline mr-2" />
            API & Webhooks
          </button>
        </nav>
      </div>

      {/* Stores Tab */}
      {activeTab === 'stores' && (
        <div className="space-y-6">
          {/* Connected Stores */}
          <div className="bg-white rounded-lg shadow">
            <div className="px-6 py-4 border-b flex items-center justify-between">
              <h2 className="text-lg font-semibold">Connected Stores</h2>
              <button
                onClick={() => setShowConnectStore(true)}
                className="flex items-center space-x-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
              >
                <Plus className="w-4 h-4" />
                <span>Connect Store</span>
              </button>
            </div>
            <div className="p-6">
              {connectedStores.length > 0 ? (
                <div className="space-y-4">
                  {connectedStores.map((store, index) => (
                    <div key={index} className="border rounded-lg p-4">
                      <div className="flex items-center justify-between">
                        <div className="flex items-center space-x-4">
                          <div className="p-2 bg-blue-100 rounded-lg">
                            <Store className="w-6 h-6 text-blue-600" />
                          </div>
                          <div>
                            <h3 className="font-medium text-gray-900">
                              {store.store_name || store.domain || 'Store ' + (index + 1)}
                            </h3>
                            <p className="text-sm text-gray-600">
                              Platform: <span className="capitalize">{store.platform}</span> • 
                              Products: {store.product_count || 0}
                            </p>
                            {store.domain && (
                              <p className="text-xs text-gray-500 mt-1">{store.domain}</p>
                            )}
                          </div>
                        </div>
                        <div className="flex items-center space-x-2">
                          <span className="px-3 py-1 bg-green-100 text-green-700 text-sm rounded-full">
                            Active
                          </span>
                          {(store.platform === 'shopify' || store.platform === 'wix') && (
                            <button
                              onClick={() => handleSyncProducts(store)}
                              disabled={syncingStoreId === store.id}
                              className="px-3 py-1 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 disabled:opacity-50"
                            >
                              {syncingStoreId === store.id ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Sync Products'}
                            </button>
                          )}
                          <button
                            onClick={() => handleDeleteStore(store)}
                            className="px-3 py-1 bg-red-600 text-white text-sm rounded hover:bg-red-700"
                          >
                            Delete
                          </button>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-center py-12">
                  <Store className="w-12 h-12 text-gray-400 mx-auto mb-4" />
                  <p className="text-gray-600 mb-4">No stores connected yet</p>
                  <button
                    onClick={() => setShowConnectStore(true)}
                    className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
                  >
                    Connect Your First Store
                  </button>
                </div>
              )}
            </div>
          </div>

          {/* Connect Store Modal - supports all platforms */}
          <ConnectStoreModal
            isOpen={showConnectStore}
            onClose={() => setShowConnectStore(false)}
            onSuccess={handleStoreConnected}
            merchantId={merchantId}
          />
        </div>
      )}

      {/* PSPs Tab */}
      {activeTab === 'psps' && (
        <div className="space-y-6">
          {/* Connected PSPs */}
          <div className="bg-white rounded-lg shadow">
            <div className="px-6 py-4 border-b flex items-center justify-between">
              <h2 className="text-lg font-semibold">Payment Processors</h2>
              <button
                onClick={() => setShowConnectPSP(true)}
                className="flex items-center space-x-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
              >
                <Plus className="w-4 h-4" />
                <span>Connect PSP</span>
              </button>
            </div>
            <div className="p-6">
              {connectedPSPs.filter(p => p.is_active).length > 0 ? (
                <div className="space-y-4">
                  {connectedPSPs.filter(p => p.is_active).map((psp) => (
                    <div key={psp.id} className="border rounded-lg p-4">
                      <div className="flex items-center justify-between">
                        <div className="flex items-center space-x-4">
                          <div className="p-2 bg-purple-100 rounded-lg">
                            <CreditCard className="w-6 h-6 text-purple-600" />
                          </div>
                          <div>
                            <h3 className="font-medium text-gray-900">{psp.name}</h3>
                            <p className="text-sm text-gray-600">
                              Success Rate: {psp.success_rate || 0}% • 
                              Volume: ${psp.volume_today || 0}
                            </p>
                          </div>
                        </div>
                        <div className="flex items-center space-x-2">
                          <span className="px-3 py-1 bg-green-100 text-green-700 text-sm rounded-full">
                            Active
                          </span>
                          <button
                            onClick={() => handleTestPSP(psp.id)}
                            disabled={testing === psp.id}
                            className="px-3 py-1 border rounded text-sm hover:bg-gray-50 disabled:opacity-50"
                          >
                            {testing === psp.id ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Test'}
                          </button>
                          <button
                            onClick={() => handleDeletePSP(psp)}
                            className="px-3 py-1 bg-red-600 text-white text-sm rounded hover:bg-red-700"
                          >
                            Delete
                          </button>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-center py-12">
                  <CreditCard className="w-12 h-12 text-gray-400 mx-auto mb-4" />
                  <p className="text-gray-600 mb-4">No payment processors connected</p>
                  <button
                    onClick={() => setShowConnectPSP(true)}
                    className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
                  >
                    Connect Your PSP
                  </button>
                </div>
              )}
            </div>
          </div>

          {/* Multi-PSP Benefits */}
          <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
            <h3 className="font-medium text-blue-900 mb-2">💡 Why Connect Multiple PSPs?</h3>
            <ul className="text-sm text-blue-800 space-y-1">
              <li>• Automatic fallback when one PSP fails</li>
              <li>• Higher overall success rates (up to 98%)</li>
              <li>• Geographic routing for better acceptance</li>
              <li>• Lower fees through competition</li>
            </ul>
          </div>

          {/* Connect PSP - Provider Selection or Config Form */}
          {showConnectPSP && !selectedPSPProvider && (
            <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
              <div className="bg-white rounded-lg p-6 max-w-md w-full max-h-[80vh] overflow-y-auto">
                <h3 className="text-lg font-semibold mb-4">Select Payment Processor</h3>
                <div className="space-y-3">
                  {['Stripe', 'PayPal', 'Adyen', 'Checkout.com', 'Square', 'Mollie', 'Braintree'].map((psp) => (
                    <button
                      key={psp}
                      onClick={() => setSelectedPSPProvider(psp)}
                      className="w-full p-4 border rounded-lg hover:bg-gray-50 text-left transition-colors"
                    >
                      <h4 className="font-medium">{psp}</h4>
                      <p className="text-sm text-gray-600">Configure {psp} payment processing</p>
                    </button>
                  ))}
                </div>
                <button
                  onClick={() => setShowConnectPSP(false)}
                  className="mt-4 w-full py-2 border rounded-lg hover:bg-gray-50"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
          
          {/* PSP Config Form */}
          {selectedPSPProvider && (
            <PSPConfigForm
              provider={selectedPSPProvider}
              merchantId={merchantId}
              onClose={() => {
                setSelectedPSPProvider('');
                setShowConnectPSP(false);
              }}
              onSuccess={() => {
                setSelectedPSPProvider('');
                setShowConnectPSP(false);
                handlePSPConnected();
              }}
              apiClient={apiClient}
            />
          )}
        </div>
      )}

      {/* Routing Tab */}
      {activeTab === 'routing' && (
        <div className="space-y-6">
          <PSPRoutingConfig connectedPSPs={connectedPSPs} />
        </div>
      )}

      {/* Webhooks Tab */}
      {activeTab === 'webhooks' && (
        <div className="space-y-6">
          {/* API Key */}
          <div className="bg-white rounded-lg shadow p-6">
            <h2 className="text-lg font-semibold mb-4">API Credentials</h2>
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">Your API Key</label>
                <div className="flex items-center space-x-2">
                  <input
                    type="text"
                    value={apiKey}
                    readOnly
                    className="flex-1 px-3 py-2 bg-gray-50 border rounded-lg font-mono text-sm"
                  />
                  <button
                    onClick={() => copyToClipboard(apiKey)}
                    className="px-3 py-2 border rounded-lg hover:bg-gray-50"
                  >
                    <Copy className="w-4 h-4" />
                  </button>
                </div>
              </div>
            </div>
          </div>

          {/* Webhook Configuration */}
          <div className="bg-white rounded-lg shadow p-6">
            <h2 className="text-lg font-semibold mb-4">Webhook Configuration</h2>
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">Webhook URL</label>
                <div className="flex items-center space-x-2">
                  <input
                    type="text"
                    value={webhookConfig?.url || ''}
                    placeholder="https://your-domain.com/webhooks/pivota"
                    className="flex-1 px-3 py-2 border rounded-lg"
                    readOnly
                  />
                  <button
                    onClick={handleSaveWebhook}
                    className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
                  >
                    Configure
                  </button>
                </div>
              </div>
              
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">Events</label>
                <div className="space-y-2">
                  {['order.created', 'payment.completed', 'payment.failed', 'product.out_of_stock'].map((event) => (
                    <label key={event} className="flex items-center space-x-2">
                      <input
                        type="checkbox"
                        checked={webhookConfig?.events?.includes(event) || false}
                        readOnly
                        className="rounded"
                      />
                      <span className="text-sm">{event}</span>
                    </label>
                  ))}
                </div>
              </div>
            </div>
          </div>

          {/* Code Examples */}
          <div className="bg-white rounded-lg shadow p-6">
            <h2 className="text-lg font-semibold mb-4">Quick Start</h2>
            <div className="bg-gray-900 text-gray-100 p-4 rounded-lg overflow-x-auto">
              <pre className="text-sm">
{`// Example: Create an order
curl -X POST https://api.pivota.cc/orders \\
  -H "Authorization: Bearer ${apiKey}" \\
  -H "Content-Type: application/json" \\
  -d '{
    "customer_email": "customer@example.com",
    "items": [
      {
        "product_id": "prod_123",
        "quantity": 2
      }
    ]
  }'`}
              </pre>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
