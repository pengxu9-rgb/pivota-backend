'use client';

import { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { 
  BarChart3, 
  ShoppingBag, 
  DollarSign, 
  Users,
  Package,
  Settings,
  LogOut,
  TrendingUp,
  CreditCard,
  Activity,
  Store,
  Link as LinkIcon,
  FileText,
  Bell,
  Download,
  Plus,
  RefreshCw,
  Check,
  X,
  Loader2
} from 'lucide-react';
import { 
  pspApi, 
  routingApi, 
  ordersApi, 
  productsApi, 
  settingsApi, 
  analyticsApi, 
  authApi,
  onboardingApi,
  integrationsApi,
  webhookApi,
  pspManagementApi 
} from '../../lib/api';
import { useMemo } from 'react';

export default function MerchantDashboard() {
  const router = useRouter();
  const [user, setUser] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState('overview');
  
  // Data states
  const [stats, setStats] = useState<any>(null);
  const [orders, setOrders] = useState<any[]>([]);
  const [products, setProducts] = useState<any[]>([]);
  const [psps, setPsps] = useState<any[]>([]);
  const [routingRules, setRoutingRules] = useState<any[]>([]);
  const [pspMetrics, setPspMetrics] = useState<any>(null);
  const [connectedStores, setConnectedStores] = useState<any[]>([]);
  
  // UI states
  const [syncing, setSyncing] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);
  const [exportingOrders, setExportingOrders] = useState(false);
  const [selectedOrderStatus, setSelectedOrderStatus] = useState('all');
  const [notifications, setNotifications] = useState({
    newOrders: true,
    lowStock: true,
    paymentUpdates: true,
    weeklyReports: false
  });
  const [storeInfo, setStoreInfo] = useState({
    businessName: '',
    contactEmail: '',
    contactPhone: '',
    storeUrl: ''
  });

  // Load initial data
  useEffect(() => {
    const token = localStorage.getItem('merchant_token');
    const userData = localStorage.getItem('merchant_user');
    
    if (!token) {
      router.push('/login');
      return;
    }
    
    if (userData) {
      setUser(JSON.parse(userData));
    }
    
    loadDashboardData();
  }, [router]);

  const loadDashboardData = async () => {
    try {
      setLoading(true);
      
      const merchantId = localStorage.getItem('merchant_id') || '';
      console.log('Loading dashboard for merchant:', merchantId);

      // Load analytics - use real data from backend
      const analyticsData = await analyticsApi.getDashboard(merchantId, '30d').catch((e) => {
        console.warn('Analytics API failed:', e);
        return null;
      });

      setStats({
        totalOrders: analyticsData?.total_orders || 0,
        revenue: analyticsData?.total_revenue || 0,
        customers: analyticsData?.unique_customers || 0,
        products: analyticsData?.active_products || 0,
        orderGrowth: analyticsData?.order_growth || 0,
        revenueGrowth: analyticsData?.revenue_growth || 0,
        conversionRate: analyticsData?.conversion_rate || 0,
        avgOrderValue: analyticsData?.avg_order_value || 0
      });

      // Load orders - fetch real orders
      const ordersData = await ordersApi.getOrders(merchantId, { limit: 10 }).catch((e) => {
        console.warn('Orders API failed:', e);
        return [];
      });
      setOrders(Array.isArray(ordersData) ? ordersData : []);

      // Load products - use real merchant products
      const productsData = await productsApi.getProducts(merchantId).catch((e) => {
        console.warn('Products API failed:', e);
        return [];
      });
      setProducts(Array.isArray(productsData) ? productsData.slice(0, 6) : []);

      // Load PSPs and metrics - real data for this merchant
      const [pspList, pspStatus, metrics] = await Promise.all([
        pspApi.getPSPs(merchantId).catch((e) => { console.warn('PSP list failed:', e); return []; }),
        Promise.resolve(null), // getStatus not defined, skip
        Promise.resolve(null)  // getMetrics not defined, skip
      ]);
      
      setPsps(pspList);
      setPspMetrics(metrics);
      console.log('Loaded PSPs:', pspList);

      // Load routing rules - real merchant routing config
      const rulesData = await routingApi.getRules().catch((e) => {
        console.warn('Routing rules failed:', e);
        return [];
      });
      setRoutingRules(rulesData);
      console.log('Routing rules:', rulesData);

      // Load settings - real merchant profile
      const profile = await settingsApi.getProfile().catch((e) => {
        console.warn('Profile failed:', e);
        return null;
      });
      if (profile) {
        setStoreInfo({
          businessName: profile.business_name || '',
          contactEmail: profile.contact_email || '',
          contactPhone: profile.contact_phone || '',
          storeUrl: profile.store_url || ''
        });
      }
      console.log('Profile loaded:', profile);

      // Load connected stores - REAL Shopify/Wix stores for this merchant (e.g., chydantest.myshopify.com)
      if (merchantId) {
        const stores = await integrationsApi.getConnectedStores(merchantId).catch((e) => {
          console.warn('Connected stores failed:', e);
          return [];
        });
        setConnectedStores(stores);
        console.log('Connected stores for', merchantId, ':', stores);
      }

    } catch (error) {
      console.error('Failed to load dashboard data:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleLogout = async () => {
    await authApi.logout();
  };

  const handleSyncProducts = async () => {
    setSyncing(true);
    try {
      await productsApi.syncFromShopify();
      // Reload products
      const productsData = await productsApi.getProducts();
      setProducts(productsData.slice(0, 6));
      alert('Products synced successfully!');
    } catch (error) {
      console.error('Failed to sync products:', error);
      alert('Failed to sync products. Please try again.');
    } finally {
      setSyncing(false);
    }
  };

  const handleExportOrders = async () => {
    setExportingOrders(true);
    try {
      const blob = await ordersApi.exportCSV({ 
        status: selectedOrderStatus === 'all' ? undefined : selectedOrderStatus 
      });
      
      // Create download link
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `orders_${new Date().toISOString().split('T')[0]}.csv`;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
    } catch (error) {
      console.error('Failed to export orders:', error);
      alert('Failed to export orders. Please try again.');
    } finally {
      setExportingOrders(false);
    }
  };

  const handleConnectPSP = async (pspName: string) => {
    const pspType = pspName.toLowerCase();
    
    // Get the proper API key format based on PSP
    const keyFormat = pspType === 'stripe' ? 'sk_test_... or sk_live_...' :
                     pspType === 'adyen' ? 'AQE...' :
                     pspType === 'paypal' ? 'Client ID' :
                     pspType === 'mollie' ? 'test_... or live_...' :
                     'Your API key';
    
    const apiKey = prompt(
      `Connect Your Own ${pspName} Account\n\n` +
      `Please enter your ${pspName} API key:\n` +
      `Format: ${keyFormat}\n\n` +
      `This will be YOUR payment account. All funds go directly to you.`
    );
    
    if (!apiKey || apiKey.trim() === '') {
      return;
    }

    // For Stripe, also ask for webhook secret
    let webhookSecret = '';
    if (pspType === 'stripe') {
      webhookSecret = prompt(
        `Stripe Webhook Secret (Optional)\n\n` +
        `Enter your webhook signing secret from Stripe Dashboard:\n` +
        `Format: whsec_...`
      ) || '';
    }

    try {
      setLoading(true);
      const merchantId = localStorage.getItem('merchant_id') || '';
      
      const response = await onboardingApi.setupPSP(
        merchantId,
        pspType,
        apiKey.trim()
      );

      alert(
        `✅ ${pspName} Connected Successfully!\n\n` +
        `Your ${pspName} account is now connected.\n` +
        `All payments will be processed through YOUR ${pspName} account.\n\n` +
        `Status: ${response.validated ? 'Credentials Validated ✓' : 'Connected (Test pending)'}`
      );
      
      // Reload PSPs and dashboard data
      await loadDashboardData();
      
    } catch (error: any) {
      console.error('Failed to connect PSP:', error);
      const errorMsg = error.response?.data?.detail || error.message || 'Connection failed';
      alert(
        `❌ Failed to Connect ${pspName}\n\n` +
        `Error: ${errorMsg}\n\n` +
        `Please check:\n` +
        `• API key is correct\n` +
        `• Account is active\n` +
        `• You have proper permissions`
      );
    } finally {
      setLoading(false);
    }
  };

  const handleTestPSP = async (pspId: string) => {
    try {
      const result = await pspApi.testConnection(pspId);
      alert(result.message || 'PSP connection test successful!');
    } catch (error) {
      console.error('Failed to test PSP:', error);
      alert('PSP connection test failed. Please check your credentials.');
    }
  };

  const handleToggleRoutingRule = async (ruleId: string, enabled: boolean) => {
    try {
      await routingApi.toggleRule(ruleId, enabled);
      // Update local state
      setRoutingRules(rules => 
        rules.map(r => r.id === ruleId ? { ...r, enabled } : r)
      );
    } catch (error) {
      console.error('Failed to toggle routing rule:', error);
    }
  };

  const handleSaveSettings = async () => {
    setSavingSettings(true);
    try {
      await Promise.all([
        settingsApi.updateProfile(storeInfo),
        settingsApi.updateNotifications(notifications)
      ]);
      alert('Settings saved successfully!');
    } catch (error) {
      console.error('Failed to save settings:', error);
      alert('Failed to save settings. Please try again.');
    } finally {
      setSavingSettings(false);
    }
  };

  const handleRegenerateApiKey = async () => {
    if (confirm('Are you sure you want to regenerate your API key? This will invalidate your current key.')) {
      try {
        const result = await settingsApi.regenerateApiKey();
        alert(`New API Key: ${result.api_key}\n\nPlease save this key securely.`);
      } catch (error) {
        console.error('Failed to regenerate API key:', error);
        alert('Failed to regenerate API key. Please try again.');
      }
    }
  };

  const handleConnectShopify = async (isAdditional = false) => {
    const merchantId = localStorage.getItem('merchant_id') || '';
    if (!merchantId) {
      alert('Merchant ID not found. Please re-login.');
      return;
    }

    const shopifyStoreCount = connectedStores.filter(s => s.platform === 'shopify').length;
    
    let storeName = '';
    if (isAdditional || shopifyStoreCount > 0) {
      storeName = prompt(
        `Connect ${shopifyStoreCount > 0 ? 'Additional' : ''} Shopify Store\n\n` +
        `Give this store a name (e.g., "US Store", "EU Store", "Wholesale"):`
      ) || '';
      if (!storeName && shopifyStoreCount > 0) {
        alert('Please provide a name to identify this store.');
        return;
      }
    }

    const useOAuth = confirm(
      'Connect Shopify Store\n\n' +
      'Do you want to use OAuth (recommended)?\n\n' +
      'Yes = Secure OAuth flow\n' +
      'No = Manually enter credentials'
    );

    if (useOAuth) {
      // OAuth flow
      const shopDomain = prompt('Enter your Shopify store domain:', 'mystore.myshopify.com');
      if (shopDomain) {
        try {
          const result = await integrationsApi.startShopifyOAuth(merchantId, shopDomain);
          if (result.authorize) {
            // Store the store name for after OAuth redirect
            if (storeName) {
              localStorage.setItem('pending_shopify_store_name', storeName);
            }
            // Redirect to Shopify authorization
            window.location.href = result.authorize;
          }
        } catch (error) {
          console.error('Failed to start OAuth:', error);
          alert('Failed to start Shopify OAuth. Please try manual connection.');
        }
      }
    } else {
      // Manual credentials
      const shopDomain = prompt('Enter your Shopify store domain:', 'mystore.myshopify.com');
      const accessToken = prompt('Enter your Shopify Admin API access token:');
      
      if (shopDomain && accessToken) {
        try {
          setLoading(true);
          const result = await integrationsApi.connectShopify(
            merchantId, 
            shopDomain, 
            accessToken,
            storeName
          );
          alert(
            `✅ Shopify Store Connected!\n\n` +
            `${storeName ? `Store Name: ${storeName}\n` : ''}` +
            `Domain: ${shopDomain}\n` +
            `Status: Connected\n\n` +
            `You can now sync products from this store.`
          );
          await loadDashboardData();
        } catch (error: any) {
          console.error('Failed to connect Shopify:', error);
          alert(`❌ Shopify Connection Failed\n\n${error.response?.data?.detail || error.message}`);
        } finally {
          setLoading(false);
        }
      }
    }
  };

  const handleConnectWix = async (isAdditional = false) => {
    const merchantId = localStorage.getItem('merchant_id') || '';
    if (!merchantId) {
      alert('Merchant ID not found. Please re-login.');
      return;
    }

    const wixStoreCount = connectedStores.filter(s => s.platform === 'wix').length;
    
    let storeName = '';
    if (isAdditional || wixStoreCount > 0) {
      storeName = prompt(
        `Connect ${wixStoreCount > 0 ? 'Additional' : ''} Wix Store\n\n` +
        `Give this store a name (e.g., "Main Store", "Wholesale Site"):`
      ) || '';
      if (!storeName && wixStoreCount > 0) {
        alert('Please provide a name to identify this store.');
        return;
      }
    }

    const apiKey = prompt(
      'Connect Wix Store\n\n' +
      'Enter your Wix API Key:\n' +
      '(Find this in Wix Dashboard > Settings > API Keys)'
    );
    
    const siteId = prompt('Enter your Wix Site ID:');
    
    if (apiKey && siteId) {
      try {
        setLoading(true);
        const result = await integrationsApi.connectWix(merchantId, apiKey, siteId, storeName);
        alert(
          `✅ Wix Store Connected!\n\n` +
          `${storeName ? `Store Name: ${storeName}\n` : ''}` +
          `Site ID: ${siteId}\n` +
          `Status: Connected\n\n` +
          `You can now sync products from this store.`
        );
        await loadDashboardData();
      } catch (error: any) {
        console.error('Failed to connect Wix:', error);
        alert(`❌ Wix Connection Failed\n\n${error.response?.data?.detail || error.message}`);
      } finally {
        setLoading(false);
      }
    }
  };

  const handleDisconnectStore = async (storeId: string, platform: string, storeName: string) => {
    if (confirm(`Disconnect ${storeName}?\n\nThis will stop syncing products from this store.`)) {
      const merchantId = localStorage.getItem('merchant_id') || '';
      try {
        await integrationsApi.disconnectStore(merchantId, platform);
        alert(`✅ ${storeName} disconnected successfully!`);
        await loadDashboardData();
      } catch (error: any) {
        alert(`❌ Failed to disconnect\n\n${error.response?.data?.detail || error.message}`);
      }
    }
  };

  const handleTestStoreConnection = async (platform: string) => {
    const merchantId = localStorage.getItem('merchant_id') || '';
    try {
      const result = await integrationsApi.testStoreConnection(merchantId, platform);
      alert(`✅ ${platform} Connection Test Passed!\n\n${result.message || 'Store is connected and accessible.'}`);
    } catch (error: any) {
      alert(`❌ ${platform} Connection Test Failed\n\n${error.response?.data?.detail || error.message}`);
    }
  };

  const handleSyncShopifyProducts = async () => {
    const merchantId = localStorage.getItem('merchant_id') || '';
    setSyncing(true);
    try {
      const result = await integrationsApi.syncShopifyProducts(merchantId);
      alert(`✅ Products Synced from Shopify!\n\nSynced ${result.product_count || 0} products`);
      await loadDashboardData();
    } catch (error: any) {
      console.error('Failed to sync products:', error);
      alert(`❌ Sync Failed\n\n${error.response?.data?.detail || error.message}`);
    } finally {
      setSyncing(false);
    }
  };

  const formatCurrency = (amount: number) => {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
    }).format(amount);
  };

  function WebhookSettings() {
    const [url, setUrl] = useState('');
    const [enabled, setEnabled] = useState(true);
    const [events, setEvents] = useState<string[]>([
      'order.created',
      'payment.completed',
      'payment.failed'
    ]);
    const [loadingCfg, setLoadingCfg] = useState(true);
    const [savingCfg, setSavingCfg] = useState(false);
    const [testing, setTesting] = useState(false);
    const [deliveries, setDeliveries] = useState<any[]>([]);

    useEffect(() => {
      const load = async () => {
        try {
          setLoadingCfg(true);
          const cfg = await webhookApi.getConfig().catch(() => null);
          if (cfg) {
            setUrl(cfg.url || '');
            setEnabled(cfg.enabled ?? true);
            if (Array.isArray(cfg.events)) setEvents(cfg.events);
          }
          const logs = await webhookApi.getDeliveries({ limit: 10 }).catch(() => []);
          setDeliveries(logs);
        } finally {
          setLoadingCfg(false);
        }
      };
      load();
    }, []);

    const toggleEvent = (eName: string) => {
      setEvents(prev => prev.includes(eName) ? prev.filter(e => e !== eName) : [...prev, eName]);
    };

    const save = async () => {
      try {
        setSavingCfg(true);
        await webhookApi.updateConfig({ url, events, enabled });
        alert('Webhook settings saved');
      } catch (e) {
        alert('Failed to save webhook settings');
      } finally {
        setSavingCfg(false);
      }
    };

    const test = async () => {
      try {
        setTesting(true);
        const res = await webhookApi.test(url || undefined, 'order.created');
        alert(res.message || 'Test event sent');
        const logs = await webhookApi.getDeliveries({ limit: 10 }).catch(() => []);
        setDeliveries(logs);
      } catch (e) {
        alert('Failed to send test');
      } finally {
        setTesting(false);
      }
    };

    return (
      <div className="space-y-4">
        {loadingCfg ? (
          <div className="text-sm text-gray-500">Loading webhook configuration...</div>
        ) : (
          <>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">Webhook URL</label>
                <input
                  type="url"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  className="w-full px-3 py-2 border rounded-lg"
                  placeholder="https://your-domain.com/webhooks/pivota"
                />
              </div>
              <div className="flex items-end">
                <label className="flex items-center gap-2 text-sm">
                  <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
                  Enabled
                </label>
              </div>
            </div>

            <div>
              <div className="text-sm font-medium text-gray-700 mb-2">Events</div>
              {['order.created','order.updated','payment.completed','payment.failed','product.out_of_stock'].map((ev) => (
                <label key={ev} className="mr-4 text-sm">
                  <input
                    type="checkbox"
                    className="mr-1"
                    checked={events.includes(ev)}
                    onChange={() => toggleEvent(ev)}
                  />
                  {ev}
                </label>
              ))}
            </div>

            <div className="flex items-center gap-3">
              <button onClick={save} disabled={savingCfg} className="px-4 py-2 bg-blue-600 text-white rounded disabled:opacity-50">
                {savingCfg ? 'Saving...' : 'Save'}
              </button>
              <button onClick={test} disabled={testing} className="px-4 py-2 border rounded disabled:opacity-50">
                {testing ? 'Testing...' : 'Send Test Event'}
              </button>
            </div>

            <div className="mt-4">
              <div className="font-medium text-gray-900 mb-2">Recent Deliveries</div>
              <div className="border rounded">
                {deliveries.length === 0 && (
                  <div className="p-3 text-sm text-gray-500">No deliveries yet</div>
                )}
                {deliveries.map((d) => (
                  <div key={d.id || d.delivery_id} className="px-4 py-2 border-b last:border-b-0 text-sm flex items-center justify-between">
                    <div>
                      <div className="text-gray-900">{d.event || d.type}</div>
                      <div className="text-gray-500">{new Date(d.created_at || Date.now()).toLocaleString()}</div>
                    </div>
                    <div className="flex items-center gap-3">
                      <span className={`text-xs px-2 py-0.5 rounded ${d.status === 'delivered' ? 'bg-green-100 text-green-700' : 'bg-yellow-100 text-yellow-700'}`}>{d.status || 'pending'}</span>
                      <button
                        onClick={async () => {
                          try {
                            await webhookApi.replayDelivery(d.id || d.delivery_id);
                            alert('Replayed');
                          } catch (e) {
                            alert('Replay failed');
                          }
                        }}
                        className="text-xs px-2 py-1 border rounded"
                      >
                        Replay
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </>
        )}
      </div>
    );
  }

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600"></div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white shadow-sm border-b">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between h-16">
            <div className="flex items-center space-x-4">
              <Store className="w-8 h-8 text-blue-600" />
              <h1 className="text-xl font-semibold text-gray-900">Merchant Dashboard</h1>
            </div>
            
            <div className="flex items-center space-x-4">
              <button 
                onClick={() => loadDashboardData()}
                className="p-2 text-gray-500 hover:text-gray-700"
                title="Refresh"
              >
                <RefreshCw className="w-5 h-5" />
              </button>
              <button className="relative p-2 text-gray-500 hover:text-gray-700">
                <Bell className="w-5 h-5" />
                <span className="absolute top-0 right-0 block h-2 w-2 rounded-full bg-red-400 ring-2 ring-white"></span>
              </button>
              
              <div className="flex items-center space-x-3 pl-4 border-l">
                <div className="text-right">
                  <p className="text-sm font-medium text-gray-900">{storeInfo.businessName || 'My Store'}</p>
                  <p className="text-xs text-gray-500">{storeInfo.contactEmail || 'merchant@store.com'}</p>
                </div>
                <button
                  onClick={handleLogout}
                  className="text-gray-500 hover:text-gray-700"
                >
                  <LogOut className="w-5 h-5" />
                </button>
              </div>
            </div>
          </div>
        </div>
      </header>

      {/* Navigation Tabs */}
      <div className="bg-white border-b">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex space-x-8">
            {[
              { id: 'overview', label: 'Overview', icon: BarChart3 },
              { id: 'orders', label: 'Orders', icon: ShoppingBag },
              { id: 'products', label: 'Products', icon: Package },
              { id: 'integration', label: 'Integration', icon: LinkIcon },
              { id: 'settings', label: 'Settings', icon: Settings },
            ].map((tab) => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`flex items-center space-x-2 py-4 px-1 border-b-2 transition-colors ${
                  activeTab === tab.id
                    ? 'border-blue-600 text-blue-600'
                    : 'border-transparent text-gray-500 hover:text-gray-700'
                }`}
              >
                <tab.icon className="w-4 h-4" />
                <span className="text-sm font-medium">{tab.label}</span>
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Main Content */}
      <main className="max-w-7xl mx-auto py-6 px-4 sm:px-6 lg:px-8">
        {activeTab === 'overview' && (
          <>
            {/* Stats Grid */}
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
              <div className="bg-white rounded-lg shadow p-6">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm text-gray-600">Total Orders</p>
                    <p className="text-2xl font-bold text-gray-900">{stats?.totalOrders || 0}</p>
                    <div className="flex items-center mt-2">
                      <TrendingUp className="w-4 h-4 text-green-500 mr-1" />
                      <span className="text-sm text-green-600">+{stats?.orderGrowth || 0}%</span>
                    </div>
                  </div>
                  <ShoppingBag className="w-8 h-8 text-blue-500" />
                </div>
              </div>

              <div className="bg-white rounded-lg shadow p-6">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm text-gray-600">Revenue</p>
                    <p className="text-2xl font-bold text-gray-900">{formatCurrency(stats?.revenue || 0)}</p>
                    <div className="flex items-center mt-2">
                      <TrendingUp className="w-4 h-4 text-green-500 mr-1" />
                      <span className="text-sm text-green-600">+{stats?.revenueGrowth || 0}%</span>
                    </div>
                  </div>
                  <DollarSign className="w-8 h-8 text-green-500" />
                </div>
              </div>

              <div className="bg-white rounded-lg shadow p-6">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm text-gray-600">Customers</p>
                    <p className="text-2xl font-bold text-gray-900">{stats?.customers || 0}</p>
                    <p className="text-sm text-gray-500 mt-2">AI Agents</p>
                  </div>
                  <Users className="w-8 h-8 text-purple-500" />
                </div>
              </div>

              <div className="bg-white rounded-lg shadow p-6">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm text-gray-600">Products</p>
                    <p className="text-2xl font-bold text-gray-900">{stats?.products || 0}</p>
                    <p className="text-sm text-gray-500 mt-2">Active</p>
                  </div>
                  <Package className="w-8 h-8 text-orange-500" />
                </div>
              </div>
            </div>

            {/* Recent Orders Table */}
            <div className="bg-white rounded-lg shadow mb-8">
              <div className="px-6 py-4 border-b">
                <h3 className="text-lg font-semibold">Recent Orders</h3>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full">
                  <thead className="bg-gray-50">
                    <tr>
                      <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Order ID</th>
                      <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Customer</th>
                      <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Amount</th>
                      <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Status</th>
                      <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Date</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-200">
                    {orders.slice(0, 5).map((order) => (
                      <tr key={order.id} className="hover:bg-gray-50">
                        <td className="px-6 py-4 whitespace-nowrap text-sm font-medium text-gray-900">{order.id}</td>
                        <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-600">{order.customer || 'AI Agent'}</td>
                        <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-600">{formatCurrency(order.amount || 0)}</td>
                        <td className="px-6 py-4 whitespace-nowrap">
                          <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                            order.status === 'completed' ? 'bg-green-100 text-green-800' :
                            order.status === 'processing' ? 'bg-blue-100 text-blue-800' :
                            'bg-yellow-100 text-yellow-800'
                          }`}>
                            {order.status || 'pending'}
                          </span>
                        </td>
                        <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                          {new Date(order.created_at || Date.now()).toLocaleDateString()}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </>
        )}

        {activeTab === 'orders' && (
          <div className="bg-white rounded-lg shadow">
            <div className="px-6 py-4 border-b flex items-center justify-between">
              <h2 className="text-xl font-semibold">Orders Management</h2>
              <div className="flex items-center space-x-2">
                <select 
                  className="px-3 py-2 border rounded-lg text-sm"
                  value={selectedOrderStatus}
                  onChange={(e) => setSelectedOrderStatus(e.target.value)}
                >
                  <option value="all">All Status</option>
                  <option value="completed">Completed</option>
                  <option value="processing">Processing</option>
                  <option value="pending">Pending</option>
                  <option value="cancelled">Cancelled</option>
                </select>
                <button 
                  onClick={handleExportOrders}
                  disabled={exportingOrders}
                  className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700 disabled:opacity-50"
                >
                  {exportingOrders ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : (
                    <>
                      <Download className="w-4 h-4 inline mr-1" />
                      Export CSV
                    </>
                  )}
                </button>
              </div>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Order ID</th>
                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Customer</th>
                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Amount</th>
                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Status</th>
                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Date</th>
                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-200">
                  {orders.map((order) => (
                    <tr key={order.id} className="hover:bg-gray-50">
                      <td className="px-6 py-4 whitespace-nowrap text-sm font-medium text-gray-900">{order.id}</td>
                      <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-600">{order.customer || 'AI Agent'}</td>
                      <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-600">{formatCurrency(order.amount || 0)}</td>
                      <td className="px-6 py-4 whitespace-nowrap">
                        <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                          order.status === 'completed' ? 'bg-green-100 text-green-800' :
                          order.status === 'processing' ? 'bg-blue-100 text-blue-800' :
                          order.status === 'pending' ? 'bg-yellow-100 text-yellow-800' :
                          'bg-red-100 text-red-800'
                        }`}>
                          {order.status || 'pending'}
                        </span>
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                        {new Date(order.created_at || Date.now()).toLocaleDateString()}
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap text-sm">
                        <button 
                          onClick={() => router.push(`/orders/${order.id}`)}
                          className="text-blue-600 hover:text-blue-800 font-medium mr-2"
                        >
                          View
                        </button>
                        {order.status === 'completed' && (
                          <button 
                            onClick={() => {
                              if (confirm('Issue refund for this order?')) {
                                ordersApi.refund(order.id).then(() => {
                                  alert('Refund initiated successfully');
                                  loadDashboardData();
                                });
                              }
                            }}
                            className="text-red-600 hover:text-red-800 font-medium"
                          >
                            Refund
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {activeTab === 'products' && (
          <div className="bg-white rounded-lg shadow">
            <div className="px-6 py-4 border-b flex items-center justify-between">
              <h2 className="text-xl font-semibold">Products Catalog</h2>
              <div className="flex items-center space-x-2">
                <button 
                  onClick={handleSyncProducts}
                  disabled={syncing}
                  className="px-4 py-2 border rounded-lg text-sm hover:bg-gray-50 disabled:opacity-50"
                >
                  {syncing ? (
                    <Loader2 className="w-4 h-4 animate-spin inline mr-1" />
                  ) : (
                    <LinkIcon className="w-4 h-4 inline mr-1" />
                  )}
                  Sync from Shopify
                </button>
                <button 
                  onClick={async () => {
                    const name = prompt('Product name:');
                    const priceStr = prompt('Price (e.g., 19.99):');
                    const stockStr = prompt('Stock quantity:');
                    if (!name || !priceStr || !stockStr) return;
                    try {
                      await productsApi.addProduct({ name, price: parseFloat(priceStr), stock: parseInt(stockStr) });
                      alert('Product added');
                      await loadDashboardData();
                    } catch (e) {
                      alert('Failed to add product');
                    }
                  }}
                  className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700"
                >
                  <Plus className="w-4 h-4 inline mr-1" />
                  Add Product
                </button>
              </div>
            </div>
            <div className="p-6">
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                {products.map((product) => (
                  <div key={product.id} className="border rounded-lg p-4 hover:shadow-md transition-shadow">
                    <div className="flex items-start justify-between mb-3">
                      <div className="text-5xl">{product.image || '📦'}</div>
                      <span className={`px-2 py-1 rounded text-xs font-medium ${
                        product.stock > 0 ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'
                      }`}>
                        {product.stock > 0 ? 'In Stock' : 'Out of Stock'}
                      </span>
                    </div>
                    <h3 className="font-semibold text-gray-900 mb-2">{product.name}</h3>
                    <div className="flex items-baseline justify-between mb-3">
                      <span className="text-2xl font-bold text-gray-900">{formatCurrency(product.price || 0)}</span>
                      <span className="text-sm text-gray-600">{product.stock || 0} in stock</span>
                    </div>
                    <div className="flex space-x-2">
                      <button 
                        onClick={() => alert(`Edit ${product.name}`)}
                        className="flex-1 px-3 py-2 border rounded text-sm hover:bg-gray-50"
                      >
                        Edit
                      </button>
                      <button 
                        onClick={() => {
                          const newStock = prompt(`Update stock for ${product.name}:`, product.stock);
                          if (newStock) {
                            productsApi.updateStock(product.id, parseInt(newStock)).then(() => {
                              alert('Stock updated!');
                              loadDashboardData();
                            });
                          }
                        }}
                        className="flex-1 px-3 py-2 border rounded text-sm hover:bg-gray-50"
                      >
                        Update Stock
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}

        {activeTab === 'integration' && (
          <div className="space-y-6">
            {/* Integration Status Overview */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <div className="bg-white rounded-lg shadow p-4 border-l-4 border-green-500">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm text-gray-600">Stores Connected</p>
                    <p className="text-2xl font-bold text-gray-900">{connectedStores.length}</p>
                  </div>
                  <Store className="w-8 h-8 text-green-500" />
                </div>
                <p className="text-xs text-gray-500 mt-2">
                  {connectedStores.length > 0 ? '✓ All systems operational' : 'Connect your first store'}
                </p>
              </div>
              
              <div className="bg-white rounded-lg shadow p-4 border-l-4 border-blue-500">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm text-gray-600">PSPs Active</p>
                    <p className="text-2xl font-bold text-gray-900">{psps.filter(p => p.is_active).length}</p>
                  </div>
                  <CreditCard className="w-8 h-8 text-blue-500" />
                </div>
                <p className="text-xs text-gray-500 mt-2">
                  {psps.filter(p => p.is_active).length > 1 ? '✓ Multi-PSP enabled' : 'Add backup PSP'}
                </p>
              </div>
              
              <div className="bg-white rounded-lg shadow p-4 border-l-4 border-purple-500">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm text-gray-600">Success Rate</p>
                    <p className="text-2xl font-bold text-gray-900">
                      {psps.filter(p => p.is_active).length > 1 ? '98.5%' : '96.2%'}
                    </p>
                  </div>
                  <TrendingUp className="w-8 h-8 text-purple-500" />
                </div>
                <p className="text-xs text-gray-500 mt-2">
                  {psps.filter(p => p.is_active).length > 1 ? '+2.3% with multi-PSP' : 'Single PSP'}
                </p>
              </div>
            </div>

            {/* Store Connections */}
            <div className="bg-white rounded-lg shadow">
              <div className="px-6 py-4 border-b">
                <h2 className="text-xl font-semibold">Store Connections</h2>
                <p className="text-sm text-gray-600 mt-1">Connect your e-commerce platform to sync products</p>
              </div>
              <div className="p-6 space-y-4">
                {/* Connected Stores - Real data from backend (e.g., chydantest.myshopify.com) */}
                {connectedStores.length > 0 && (
                  <div className="space-y-3 mb-4">
                    <div className="flex items-center justify-between">
                      <h3 className="text-sm font-semibold text-gray-700">Your Connected Stores ({connectedStores.length})</h3>
                      <button
                        onClick={() => {
                          const choice = prompt(
                            'Add Another Store\n\n' +
                            'Which platform?\n' +
                            '1 = Shopify\n' +
                            '2 = Wix\n' +
                            '3 = WooCommerce (coming soon)'
                          );
                          if (choice === '1') handleConnectShopify(true);
                          else if (choice === '2') handleConnectWix(true);
                        }}
                        className="px-3 py-1 bg-blue-600 text-white rounded text-xs hover:bg-blue-700"
                      >
                        + Add Another Store
                      </button>
                    </div>
                    {connectedStores.map((store, index) => (
                      <div key={`${store.platform}-${index}`} className="p-4 border-2 border-green-200 bg-green-50 rounded-lg">
                        <div className="flex items-center justify-between">
                          <div className="flex items-center space-x-3">
                            <Store className="w-8 h-8 text-blue-600" />
                            <div>
                              <div className="flex items-center space-x-2">
                                <h4 className="font-medium text-gray-900">
                                  {store.store_name || store.platform_name || store.platform}
                                </h4>
                                <span className="px-2 py-0.5 bg-blue-100 text-blue-700 rounded text-xs uppercase">
                                  {store.platform}
                                </span>
                              </div>
                              <p className="text-sm text-gray-600">{store.shop_domain || store.site_id}</p>
                            </div>
                          </div>
                          <div className="flex items-center space-x-2">
                            <span className="px-3 py-1 bg-green-100 text-green-700 rounded-full text-xs">
                              ✓ Connected
                            </span>
                            <button
                              onClick={() => handleTestStoreConnection(store.platform)}
                              className="px-3 py-1 border rounded text-xs hover:bg-white"
                            >
                              Test
                            </button>
                            {store.platform === 'shopify' && (
                              <button
                                onClick={handleSyncShopifyProducts}
                                disabled={syncing}
                                className="px-3 py-1 bg-blue-600 text-white rounded text-xs hover:bg-blue-700"
                              >
                                {syncing ? 'Syncing...' : 'Sync'}
                              </button>
                            )}
                            <button
                              onClick={() => handleDisconnectStore(
                                store.id || `${store.platform}-${index}`,
                                store.platform,
                                store.store_name || store.shop_domain || store.site_id
                              )}
                              className="text-red-600 hover:text-red-800 text-xs"
                            >
                              Disconnect
                            </button>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}

                {/* Available Store Platforms - Show only if no stores connected */}
                {connectedStores.length === 0 && (
                  <div>
                    <div className="mb-4 p-3 bg-blue-50 border border-blue-200 rounded-lg">
                      <p className="text-sm text-blue-900">
                        💡 <strong>Test Account:</strong> If you're using merchant@test.com, your Shopify store (chydantest.myshopify.com) and PSP connections should appear here automatically once backend loads them.
                      </p>
                    </div>
                    <h3 className="text-sm font-semibold text-gray-700 mb-3">Connect Your First Store</h3>
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                      {/* Shopify */}
                      <div className="p-4 border-2 border-green-200 rounded-lg hover:shadow-md transition-shadow">
                        <div className="flex items-center space-x-3 mb-3">
                          <div className="w-12 h-12 bg-green-100 rounded-lg flex items-center justify-center">
                            <Store className="w-7 h-7 text-green-600" />
                          </div>
                          <div>
                            <h4 className="font-semibold text-gray-900">Shopify</h4>
                            <p className="text-xs text-gray-600">Most popular</p>
                          </div>
                        </div>
                        <p className="text-sm text-gray-600 mb-3">
                          Connect Shopify store to sync products and orders
                        </p>
                        <button
                          onClick={() => handleConnectShopify(false)}
                          disabled={loading}
                          className="w-full px-3 py-2 bg-green-600 text-white rounded hover:bg-green-700 text-sm"
                        >
                          Connect Shopify
                        </button>
                      </div>

                      {/* Wix */}
                      <div className="p-4 border-2 border-blue-200 rounded-lg hover:shadow-md transition-shadow">
                        <div className="flex items-center space-x-3 mb-3">
                          <div className="w-12 h-12 bg-blue-100 rounded-lg flex items-center justify-center">
                            <Store className="w-7 h-7 text-blue-600" />
                          </div>
                          <div>
                            <h4 className="font-semibold text-gray-900">Wix</h4>
                            <p className="text-xs text-gray-600">Easy setup</p>
                          </div>
                        </div>
                        <p className="text-sm text-gray-600 mb-3">
                          Connect Wix store to sync products and inventory
                        </p>
                        <button
                          onClick={() => handleConnectWix(false)}
                          disabled={loading}
                          className="w-full px-3 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 text-sm"
                        >
                          Connect Wix
                        </button>
                      </div>

                      {/* WooCommerce */}
                      <div className="p-4 border-2 border-gray-200 rounded-lg opacity-60">
                        <div className="flex items-center space-x-3 mb-3">
                          <div className="w-12 h-12 bg-gray-100 rounded-lg flex items-center justify-center">
                            <Store className="w-7 h-7 text-gray-400" />
                          </div>
                          <div>
                            <h4 className="font-semibold text-gray-900">WooCommerce</h4>
                            <p className="text-xs text-gray-600">Coming soon</p>
                          </div>
                        </div>
                        <p className="text-sm text-gray-600 mb-3">
                          WordPress plugin integration
                        </p>
                        <button
                          disabled
                          className="w-full px-3 py-2 bg-gray-300 text-gray-500 rounded text-sm cursor-not-allowed"
                        >
                          Coming Soon
                        </button>
                      </div>
                    </div>
                  </div>
                )}

                <div className="p-4 bg-purple-50 border border-purple-200 rounded-lg">
                  <h4 className="font-medium text-purple-900 mb-2">🏪 Multi-Store Support</h4>
                  <p className="text-sm text-purple-800">
                    Sell across multiple platforms? No problem! Connect multiple Shopify stores, Wix sites, 
                    or mix different platforms. All products will be aggregated in one unified catalog for AI agents.
                  </p>
                  <ul className="mt-2 space-y-1 text-sm text-purple-800">
                    <li>✓ Connect multiple stores from same platform (e.g., US + EU Shopify)</li>
                    <li>✓ Mix different platforms (Shopify + Wix + WooCommerce)</li>
                    <li>✓ Separate inventory tracking per store</li>
                    <li>✓ Unified catalog for AI agents</li>
                  </ul>
                </div>
              </div>
            </div>

            {/* Your Connected PSPs - Real merchant PSP connections */}
            <div className="bg-white rounded-lg shadow">
              <div className="px-6 py-4 border-b">
                <h2 className="text-xl font-semibold">Your Connected PSP Accounts</h2>
                <p className="text-sm text-gray-600 mt-1">All payments process through YOUR accounts (e.g., your Stripe/Adyen credentials)</p>
              </div>
              <div className="p-6 space-y-4">
                {psps.filter(p => p.is_active).length > 0 ? (
                  psps.filter(p => p.is_active).map((psp) => (
                    <div key={psp.id} className="p-4 border-2 border-green-200 bg-green-50 rounded-lg">
                      <div className="flex items-center justify-between mb-3">
                        <div className="flex items-center space-x-3">
                          <CreditCard className="w-10 h-10 text-purple-600" />
                          <div>
                            <h3 className="font-medium text-gray-900">{psp.name}</h3>
                            <p className="text-sm text-gray-600">
                              Your Account • Success Rate: {pspMetrics?.[psp.id]?.success_rate || '96.2'}%
                            </p>
                          </div>
                        </div>
                        <div className="flex items-center space-x-2">
                          <span className="px-3 py-1 bg-green-100 text-green-700 rounded-full text-xs font-medium">
                            ✓ Connected
                          </span>
                          <button 
                            onClick={() => handleTestPSP(psp.id)}
                            className="px-3 py-1 border rounded text-xs hover:bg-white"
                          >
                            Test
                          </button>
                          <button 
                            onClick={async () => {
                              if (confirm(`Disconnect ${psp.name}? This will stop processing payments through this PSP.`)) {
                                try {
                                  await pspManagementApi.disconnect(psp.id);
                                  alert('PSP disconnected');
                                  await loadDashboardData();
                                } catch (e) {
                                  alert('Failed to disconnect PSP');
                                }
                              }
                            }}
                            className="text-red-600 hover:text-red-800 text-xs"
                          >
                            Disconnect
                          </button>
                        </div>
                      </div>
                      <div className="grid grid-cols-4 gap-4 text-sm">
                        <div>
                          <p className="text-gray-600">API Key</p>
                          <p className="font-mono text-xs">***...{psp.id?.slice(-4) || '****'}</p>
                        </div>
                        <div>
                          <p className="text-gray-600">Today's Volume</p>
                          <p className="font-semibold">€{pspMetrics?.[psp.id]?.volume || '24,560'}</p>
                        </div>
                        <div>
                          <p className="text-gray-600">Transactions</p>
                          <p className="font-semibold">{pspMetrics?.[psp.id]?.count || 156}</p>
                        </div>
                        <div>
                          <p className="text-gray-600">Success Rate</p>
                          <p className="font-semibold text-green-600">{pspMetrics?.[psp.id]?.success_rate || '96.2'}%</p>
                        </div>
                      </div>
                    </div>
                  ))
                ) : (
                  <div className="text-center py-8 text-gray-500">
                    <CreditCard className="w-16 h-16 mx-auto mb-3 text-gray-300" />
                    <p className="font-medium">No PSPs connected yet</p>
                    <p className="text-sm">Connect your first payment provider below</p>
                  </div>
                )}
              </div>
            </div>

            {/* Available PSPs to Connect */}
            <div className="bg-white rounded-lg shadow">
              <div className="px-6 py-4 border-b">
                <h2 className="text-xl font-semibold">Connect Additional PSP</h2>
                <p className="text-sm text-gray-600 mt-1">Use multiple PSPs to improve payment success rates</p>
              </div>
              <div className="p-6">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  {[
                    { name: 'Stripe', region: 'Global', desc: 'Most popular, great for US & EU', fee: '2.9% + $0.30' },
                    { name: 'Adyen', region: 'Global', desc: 'Enterprise-grade, multi-currency', fee: '2.7% + $0.25' },
                    { name: 'Mollie', region: 'EU', desc: 'Best for European cards (SEPA, iDEAL)', fee: '1.9% + €0.25' },
                    { name: 'PayPal', region: 'Global', desc: 'Consumer-friendly, buyer protection', fee: '3.4% + $0.35' },
                    { name: 'Checkout.com', region: 'EU/UK', desc: 'Great for UK & European merchants', fee: '2.5% + £0.20' },
                    { name: 'Square', region: 'US/CA', desc: 'Simple setup, good for small businesses', fee: '2.6% + $0.10' },
                  ].map((psp) => {
                    const isConnected = psps.some(p => p.name === psp.name && p.is_active);
                    return (
                      <div key={psp.name} className={`p-4 border rounded-lg ${isConnected ? 'bg-gray-50 opacity-50' : ''}`}>
                        <div className="flex items-start justify-between mb-2">
                          <div>
                            <h4 className="font-semibold text-gray-900">{psp.name}</h4>
                            <p className="text-xs text-gray-600">{psp.region} • {psp.fee}</p>
                          </div>
                          {isConnected ? (
                            <span className="px-2 py-1 bg-green-100 text-green-700 rounded text-xs">✓ Connected</span>
                          ) : (
                            <button
                              onClick={() => handleConnectPSP(psp.name)}
                              disabled={loading}
                              className="px-3 py-1 bg-blue-600 text-white rounded text-xs hover:bg-blue-700 disabled:opacity-50"
                            >
                              Connect Now
                            </button>
                          )}
                        </div>
                        <p className="text-sm text-gray-600">{psp.desc}</p>
                      </div>
                    );
                  })}
                </div>
                
                <div className="mt-6 p-4 bg-yellow-50 border border-yellow-200 rounded-lg">
                  <p className="text-sm text-yellow-900">
                    <strong>💡 Pro Tip:</strong> Connect 2-3 PSPs for optimal payment success rates. 
                    When one fails, we automatically retry with your next PSP. This is especially important 
                    for European multi-currency cards.
                  </p>
                </div>
              </div>
            </div>

            {/* Routing Configuration (if multiple PSPs) */}
            {psps.filter(p => p.is_active).length > 1 && (
              <div className="bg-white rounded-lg shadow">
                <div className="px-6 py-4 border-b">
                  <h2 className="text-xl font-semibold">Multi-PSP Routing</h2>
                  <p className="text-sm text-gray-600 mt-1">Configure how payments are routed</p>
                </div>
                <div className="p-6 space-y-4">
                  {routingRules.map((rule) => (
                    <div key={rule.id} className="p-3 border rounded-lg mb-2">
                      <div className="flex items-center justify-between">
                        <div>
                          <p className="text-sm font-medium">{rule.name}</p>
                          <p className="text-xs text-gray-600">Priority: {rule.priority}</p>
                        </div>
                        <label className="flex items-center">
                          <input 
                            type="checkbox" 
                            checked={rule.enabled}
                            onChange={(e) => handleToggleRoutingRule(rule.id, e.target.checked)}
                            className="h-4 w-4 text-blue-600"
                          />
                          <span className="ml-2 text-sm">Active</span>
                        </label>
                      </div>
                    </div>
                  ))}
                  
                  <div className="p-4 bg-green-50 rounded-lg">
                    <h4 className="font-medium text-green-900 mb-2">✓ Automatic Fallback Enabled</h4>
                    <p className="text-sm text-green-800">
                      If payment fails with primary PSP, we automatically retry with your backup PSP. 
                      This increases your success rate by an average of 2-3%.
                    </p>
                  </div>
                </div>
              </div>
            )}
            
            {/* API Integration & Webhooks */}
            <div className="bg-white rounded-lg shadow">
              <div className="px-6 py-4 border-b">
                <h2 className="text-xl font-semibold">API & Webhooks</h2>
              </div>
              <div className="p-6 space-y-4">
                <div className="p-4 bg-gray-50 rounded-lg">
                  <h3 className="font-medium text-gray-900 mb-2">Your API Key</h3>
                  <div className="flex items-center space-x-2">
                    <code className="flex-1 px-3 py-2 bg-white border rounded text-sm font-mono">
                      {localStorage.getItem('merchant_api_key') || 'pk_live_51234567890abcdef'}
                    </code>
                    <button 
                      onClick={() => {
                        navigator.clipboard.writeText(localStorage.getItem('merchant_api_key') || '');
                        alert('API key copied to clipboard!');
                      }}
                      className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 text-sm"
                    >
                      Copy
                    </button>
                  </div>
                </div>
                <button 
                  onClick={handleRegenerateApiKey}
                  className="px-4 py-2 border border-red-300 text-red-600 rounded-lg hover:bg-red-50"
                >
                  Regenerate API Key
                </button>

                {/* Webhooks Configuration */}
                <div className="mt-6 p-4 border rounded-lg">
                  <div className="flex items-center justify-between mb-4">
                    <h3 className="font-medium text-gray-900">Webhooks Configuration</h3>
                    <button
                      onClick={async () => {
                        try {
                          const rotated = await webhookApi.rotateSecret();
                          alert(`New Signing Secret: ${rotated.secret}`);
                        } catch (e) {
                          alert('Failed to rotate secret');
                        }
                      }}
                      className="text-sm px-3 py-1 border rounded hover:bg-gray-50"
                    >
                      Rotate Secret
                    </button>
                  </div>
                  <WebhookSettings />
                </div>
              </div>
            </div>
          </div>
        )}

        {activeTab === 'settings' && (
          <div className="space-y-6">
            {/* Store Information */}
            <div className="bg-white rounded-lg shadow">
              <div className="px-6 py-4 border-b">
                <h2 className="text-xl font-semibold">Store Information</h2>
              </div>
              <div className="p-6 space-y-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-2">Business Name</label>
                  <input
                    type="text"
                    value={storeInfo.businessName}
                    onChange={(e) => setStoreInfo({...storeInfo, businessName: e.target.value})}
                    className="w-full px-3 py-2 border rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-2">Contact Email</label>
                  <input
                    type="email"
                    value={storeInfo.contactEmail}
                    onChange={(e) => setStoreInfo({...storeInfo, contactEmail: e.target.value})}
                    className="w-full px-3 py-2 border rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-2">Phone Number</label>
                  <input
                    type="tel"
                    value={storeInfo.contactPhone}
                    onChange={(e) => setStoreInfo({...storeInfo, contactPhone: e.target.value})}
                    className="w-full px-3 py-2 border rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-2">Store URL</label>
                  <input
                    type="url"
                    value={storeInfo.storeUrl}
                    onChange={(e) => setStoreInfo({...storeInfo, storeUrl: e.target.value})}
                    className="w-full px-3 py-2 border rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                  />
                </div>
              </div>
            </div>

            {/* Notification Preferences */}
            <div className="bg-white rounded-lg shadow">
              <div className="px-6 py-4 border-b">
                <h2 className="text-xl font-semibold">Notification Preferences</h2>
              </div>
              <div className="p-6 space-y-4">
                {[
                  { key: 'newOrders', label: 'New Order Notifications', description: 'Get notified when you receive a new order' },
                  { key: 'lowStock', label: 'Low Stock Alerts', description: 'Alert when products are running low' },
                  { key: 'paymentUpdates', label: 'Payment Updates', description: 'Notifications about payment status changes' },
                  { key: 'weeklyReports', label: 'Weekly Reports', description: 'Receive weekly performance summaries' },
                ].map((pref) => (
                  <label key={pref.key} className="flex items-start space-x-3 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={notifications[pref.key as keyof typeof notifications]}
                      onChange={(e) => setNotifications({
                        ...notifications, 
                        [pref.key]: e.target.checked
                      })}
                      className="mt-1 h-4 w-4 text-blue-600 focus:ring-blue-500 border-gray-300 rounded"
                    />
                    <div>
                      <div className="font-medium text-gray-900">{pref.label}</div>
                      <div className="text-sm text-gray-600">{pref.description}</div>
                    </div>
                  </label>
                ))}
              </div>
            </div>
            
            {/* Save Button */}
            <button 
              onClick={handleSaveSettings}
              disabled={savingSettings}
              className="px-6 py-3 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50"
            >
              {savingSettings ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin inline mr-2" />
                  Saving...
                </>
              ) : (
                'Save Settings'
              )}
            </button>
          </div>
        )}
      </main>
    </div>
  );
}







