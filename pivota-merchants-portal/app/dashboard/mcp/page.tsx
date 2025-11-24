'use client';

import { useState, useEffect } from 'react';
import { 
  Activity, 
  CheckCircle, 
  XCircle, 
  Zap, 
  Clock,
  TrendingUp,
  RefreshCw,
  PlayCircle,
  AlertCircle,
} from 'lucide-react';
import { apiClient } from '@/lib/api-client';

export default function MCPPage() {
  const [loading, setLoading] = useState(true);
  const [mcpStatus, setMcpStatus] = useState<any>(null);
  const [testing, setTesting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [platformOnboarding, setPlatformOnboarding] = useState<any | null>(null);
  const [platformSummaries, setPlatformSummaries] = useState<any[] | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogProducts, setCatalogProducts] = useState<any[]>([]);
  const [catalogTotal, setCatalogTotal] = useState(0);
  const [catalogFilter, setCatalogFilter] = useState<'all' | 'shopify' | 'wix' | 'amazon' | 'temu'>('all');
  const [catalogSearch, setCatalogSearch] = useState('');
  const [catalogPage, setCatalogPage] = useState(1);
  const catalogPageSize = 20;

  const defaultStatus = {
    connected: false,
    platform: null,
    shop_domain: null,
    nodes: [] as any[],
    total_requests: 0,
    avg_latency: 0,
    success_rate: 0,
    last_sync: null as string | null,
    latest_sync: null as string | null,
    total_stores: 0,
    active_stores: 0,
    total_products: 0,
    active_products: 0,
  };

  useEffect(() => {
    loadMCPStatus();
  }, []);

  useEffect(() => {
    loadCatalogProducts(catalogFilter, catalogPage);
  }, [catalogFilter, catalogPage]);

  const loadMCPStatus = async () => {
    try {
      setLoading(true);
      setErrorMessage(null);

      const merchantId = localStorage.getItem('merchant_id') || '';
      console.log('Loading MCP status for merchant:', merchantId);

      if (!merchantId) {
        setMcpStatus(defaultStatus);
        setPlatformOnboarding(null);
        setPlatformSummaries(null);
        setCatalogProducts([]);
        setCatalogTotal(0);
        return;
      }

      // Try to get MCP summary from backend (fallback if 404)
      let summary = null;
      try {
        summary = await apiClient.getMcpSummary();
      } catch (err: any) {
        console.warn('MCP summary endpoint not available (404), using fallback:', err.response?.status);
        summary = null;
      }

      const stores = await apiClient.getConnectedStores(merchantId).catch((err) => {
        console.warn('Stores failed:', err);
        return [];
      });

      // Platform Onboarding v2 status (optional, behind feature flag)
      try {
        const onboardingResp = await apiClient.get(`/platform-onboarding/${merchantId}`);
        const onboardingData = onboardingResp.data || onboardingResp;
        setPlatformOnboarding(onboardingData);
      } catch (err: any) {
        if (err?.response?.status === 404) {
          setPlatformOnboarding(null);
        } else {
          console.warn('Platform onboarding status failed:', err?.response?.status || err);
        }
      }

      // Platform catalog summary from products_cache (optional)
      try {
        const platformsResp = await apiClient.get(`/products/v2/${merchantId}/platforms`);
        const body = platformsResp.data || platformsResp;
        const platforms = body?.platforms || [];
        setPlatformSummaries(Array.isArray(platforms) ? platforms : []);
      } catch (err) {
        console.warn('Platform products summary failed:', err);
        setPlatformSummaries(null);
      }

      await loadCatalogProducts(catalogFilter, catalogPage);

      if (summary && summary.connected !== undefined) {
        // Backend returned valid summary data
        setMcpStatus({
          ...defaultStatus,
          ...summary,
          nodes: summary.nodes || [],
        });
      } else {
        // Fallback: build status from stores data
        const activeStore = stores.find((s: any) => s.is_active || s.status === 'active');
        const store = activeStore || stores[0];

        if (store) {
          setMcpStatus({
            ...defaultStatus,
            connected: true,
            platform: store.platform,
            shop_domain: store.domain || store.store_name,
            total_products: store.product_count || 0,
            active_products: store.product_count || 0,
            total_requests: store.product_count || 0,
            nodes: [
              {
                id: store.id || store.store_id || `${store.platform}-fallback`,
                name: store.store_name || store.name || store.domain || 'Store',
                status: 'online',
                latency_ms: 0,
                uptime: 99.9,
                product_count: store.product_count || 0,
                domain: store.domain,
                last_sync: store.last_sync || null,
                platform: store.platform,
              },
            ],
            last_sync: store.last_sync || null,
            latest_sync: store.last_sync || null,
            total_stores: stores.length,
            active_stores: activeStore ? 1 : (store ? 1 : 0),
          });
        } else {
          setMcpStatus(defaultStatus);
        }
      }
    } catch (error) {
      console.error('Failed to load MCP status:', error);
      setErrorMessage('Failed to load MCP data. Please try again.');
      setMcpStatus(defaultStatus);
    } finally {
      setLoading(false);
    }
  };

  const loadCatalogProducts = async (
    platform: 'all' | 'shopify' | 'wix' | 'amazon' | 'temu' = catalogFilter,
    page: number = catalogPage
  ) => {
    const merchantId = localStorage.getItem('merchant_id') || '';
    if (!merchantId) {
      setCatalogProducts([]);
      setCatalogTotal(0);
      return;
    }

    setCatalogLoading(true);
    try {
      const paramsPlatform = platform === 'all' ? undefined : platform;
      const resp = await apiClient.getProductsV2({
        platform: paramsPlatform,
        limit: catalogPageSize,
        offset: (page - 1) * catalogPageSize,
      });
      setCatalogProducts(resp.products || []);
      setCatalogTotal(resp.total || 0);
    } catch (err) {
      console.warn('Catalog products load failed:', err);
      setCatalogProducts([]);
      setCatalogTotal(0);
    } finally {
      setCatalogLoading(false);
    }
  };

  const formatLatency = (value?: number | null) => {
    if (value === null || value === undefined || Number.isNaN(value)) {
      return '—';
    }
    return `${Math.round(value)}ms`;
  };

  const formatPercent = (value?: number | null) => {
    if (value === null || value === undefined || Number.isNaN(value)) {
      return '—';
    }
    const formatted = Number(value).toFixed(2);
    return `${formatted.endsWith('.00') ? formatted.slice(0, -3) : formatted}%`;
  };

  const formatDateTime = (value?: string | null) => {
    if (!value) return '—';
    try {
      return new Date(value).toLocaleString();
    } catch (e) {
      return value;
    }
  };

  const filteredCatalog = catalogProducts.filter((p) => {
    if (!catalogSearch.trim()) return true;
    const term = catalogSearch.toLowerCase();
    return (
      (p.title || '').toLowerCase().includes(term) ||
      (p.id || '').toLowerCase().includes(term) ||
      (p.platform || '').toLowerCase().includes(term)
    );
  });

  const handleTestConnection = async () => {
    setTesting(true);
    try {
      const merchantId = localStorage.getItem('merchant_id');
      if (!merchantId) {
        alert('❌ Merchant ID not found');
        return;
      }

      // Check if any store is connected
      const response = await fetch(
        `https://web-production-fedb.up.railway.app/merchant/${merchantId}/integrations`,
        {
          headers: {
            'Authorization': `Bearer ${localStorage.getItem('merchant_token')}`
          }
        }
      );

      const data = await response.json();
      const stores = data?.data?.stores || [];

      if (stores.length === 0) {
        alert('❌ MCP Connection Test Failed\n\nNo store connected. Please connect your Shopify, Wix, or other platform in Integrations page first.');
      } else {
        const activeStores = stores.filter((s: any) => s.status === 'active');
        if (activeStores.length > 0) {
          alert(`✅ MCP Connection Test Successful!\n\n${activeStores.length} store(s) connected:\n${activeStores.map((s: any) => `• ${s.platform}: ${s.domain || s.name}`).join('\n')}`);
        } else {
          alert('⚠️ Stores found but none are active. Please check your store connections.');
        }
      }
    } catch (error: any) {
      alert('❌ MCP connection test failed: ' + (error.message || 'Unknown error'));
    } finally {
      setTesting(false);
    }
  };

  const handleSyncNow = async () => {
    try {
      const merchantId = localStorage.getItem('merchant_id');
      if (!merchantId) {
        alert('❌ Merchant ID not found');
        return;
      }

      // Check which stores are connected
      const storesResponse = await fetch(
        `https://web-production-fedb.up.railway.app/merchant/${merchantId}/integrations`,
        {
          headers: {
            'Authorization': `Bearer ${localStorage.getItem('merchant_token')}`
          }
        }
      );

      const storesData = await storesResponse.json();
      const stores = storesData?.data?.stores || [];

      if (stores.length === 0) {
        alert('❌ No store connected\n\nPlease connect your store first in Integrations page.');
        return;
      }

      // Sync each connected store
      let synced = 0;
      let errors = [];

      for (const store of stores) {
        if (store.platform === 'shopify') {
          try {
            const syncResponse = await fetch(
              'https://web-production-fedb.up.railway.app/merchant/integrations/shopify/sync',
              {
                method: 'POST',
                headers: {
                  'Authorization': `Bearer ${localStorage.getItem('merchant_token')}`,
                  'Content-Type': 'application/json'
                }
              }
            );

            const syncData = await syncResponse.json();
            
            if (syncResponse.ok) {
              synced++;
            } else {
              errors.push(`${store.platform}: ${syncData.detail || 'Failed'}`);
            }
          } catch (e: any) {
            errors.push(`${store.platform}: ${e.message}`);
          }
        } else if (store.platform === 'wix') {
          try {
            const syncResponse = await fetch(
              'https://web-production-fedb.up.railway.app/merchant/integrations/wix/sync',
              {
                method: 'POST',
                headers: {
                  'Authorization': `Bearer ${localStorage.getItem('merchant_token')}`,
                  'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                  merchant_id: merchantId
                })
              }
            );

            const syncData = await syncResponse.json();
            
            if (syncResponse.ok) {
              synced++;
            } else {
              errors.push(`${store.platform}: ${syncData.detail || 'Failed'}`);
            }
          } catch (e: any) {
            errors.push(`${store.platform}: ${e.message}`);
          }
        } else {
          // Other platforms not implemented yet
          errors.push(`${store.platform}: Sync not implemented yet`);
        }
      }

      if (errors.length > 0) {
        alert(`⚠️ Sync completed with errors:\n\n✅ Synced: ${synced}\n❌ Errors:\n${errors.join('\n')}`);
      } else {
        alert(`✅ MCP sync completed!\n\n${synced} store(s) synced successfully.`);
      }
      await loadMCPStatus();
    } catch (error) {
      alert('❌ Sync failed');
    }
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
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">MCP Integration</h1>
          <p className="text-gray-600">Monitor and manage your MCP payment chain</p>
        </div>
        <div className="flex items-center space-x-2">
          <button
            onClick={loadMCPStatus}
            className="flex items-center space-x-2 px-4 py-2 border rounded-lg hover:bg-gray-50"
          >
            <RefreshCw className="w-4 h-4" />
            <span>Refresh</span>
          </button>
          <button
            onClick={handleTestConnection}
            disabled={testing}
            className="flex items-center space-x-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50"
          >
            {testing ? (
              <>
                <RefreshCw className="w-4 h-4 animate-spin" />
                <span>Testing...</span>
              </>
            ) : (
              <>
                <PlayCircle className="w-4 h-4" />
                <span>Test Connection</span>
              </>
            )}
          </button>
        </div>
      </div>

      {errorMessage && (
        <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg">
          {errorMessage}
        </div>
      )}

      {/* Connection Status */}
      <div className={`rounded-lg p-6 ${mcpStatus?.connected ? 'bg-green-50 border-2 border-green-200' : 'bg-red-50 border-2 border-red-200'}`}>
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-4">
            {mcpStatus?.connected ? (
              <CheckCircle className="w-10 h-10 text-green-600" />
            ) : (
              <XCircle className="w-10 h-10 text-red-600" />
            )}
            <div>
              <h2 className="text-xl font-bold text-gray-900">
                {mcpStatus?.connected ? 'MCP Connected' : 'MCP Disconnected'}
              </h2>
              <p className="text-sm text-gray-600">
                {mcpStatus?.platform && `Platform: ${mcpStatus.platform} • `}
                {mcpStatus?.shop_domain || 'No store connected'}
              </p>
              <p className="text-xs text-gray-500 mt-1">
                Stores: {mcpStatus?.active_stores ?? 0}/{mcpStatus?.total_stores ?? 0} • Products: {mcpStatus?.total_products ?? 0}
              </p>
            </div>
          </div>
          <button
            onClick={handleSyncNow}
            className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
          >
            Sync Now
          </button>
        </div>
      </div>

      {/* MCP Metrics */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-6">
        <div className="bg-white rounded-lg shadow p-6">
          <div className="flex items-center justify-between mb-4">
            <div className="p-2 bg-blue-100 rounded-lg">
              <Activity className="w-6 h-6 text-blue-600" />
            </div>
          </div>
          <h3 className="text-2xl font-bold text-gray-900">{mcpStatus?.total_requests || 0}</h3>
          <p className="text-sm text-gray-600">Total Requests</p>
        </div>

        <div className="bg-white rounded-lg shadow p-6">
          <div className="flex items-center justify-between mb-4">
            <div className="p-2 bg-purple-100 rounded-lg">
              <Clock className="w-6 h-6 text-purple-600" />
            </div>
          </div>
          <h3 className="text-2xl font-bold text-gray-900">{formatLatency(mcpStatus?.avg_latency ?? mcpStatus?.avg_latency_ms ?? null)}</h3>
          <p className="text-sm text-gray-600">Avg Latency</p>
        </div>

        <div className="bg-white rounded-lg shadow p-6">
          <div className="flex items-center justify-between mb-4">
            <div className="p-2 bg-green-100 rounded-lg">
              <TrendingUp className="w-6 h-6 text-green-600" />
            </div>
          </div>
          <h3 className="text-2xl font-bold text-gray-900">{formatPercent(mcpStatus?.success_rate ?? null)}</h3>
          <p className="text-sm text-gray-600">Success Rate</p>
        </div>

        <div className="bg-white rounded-lg shadow p-6">
          <div className="flex items-center justify-between mb-4">
            <div className="p-2 bg-orange-100 rounded-lg">
              <Zap className="w-6 h-6 text-orange-600" />
            </div>
          </div>
          <h3 className="text-2xl font-bold text-gray-900">{mcpStatus?.nodes?.length || 0}</h3>
          <p className="text-sm text-gray-600">Active Nodes</p>
        </div>
      </div>

      {/* MCP Nodes Status */}
      <div className="bg-white rounded-lg shadow">
        <div className="px-6 py-4 border-b">
          <h2 className="text-lg font-semibold text-gray-900">MCP Nodes</h2>
        </div>
        <div className="p-6">
          {mcpStatus?.nodes && mcpStatus.nodes.length > 0 ? (
            <div className="space-y-4">
              {mcpStatus.nodes.map((node: any) => (
                <div key={node.id} className="flex items-center justify-between p-4 border rounded-lg">
                  <div className="flex items-center space-x-4">
                    <div className={`p-2 rounded-lg ${
                      node.status === 'online' ? 'bg-green-100' : 'bg-red-100'
                    }`}>
                      {node.status === 'online' ? (
                        <CheckCircle className="w-6 h-6 text-green-600" />
                      ) : (
                        <XCircle className="w-6 h-6 text-red-600" />
                      )}
                    </div>
                    <div>
                      <h3 className="font-medium text-gray-900">{node.name}</h3>
                      <p className="text-sm text-gray-600 capitalize">Status: {node.status}</p>
                      <p className="text-xs text-gray-500">
                        {(node.platform || '').toUpperCase()} {node.domain ? `• ${node.domain}` : ''}
                      </p>
                    </div>
                  </div>
                  <div className="flex items-center space-x-6">
                    <div className="text-center">
                      <p className="text-sm text-gray-600">Latency</p>
                      <p className="font-semibold text-gray-900">{formatLatency(node.latency_ms ?? node.latency ?? null)}</p>
                    </div>
                    <div className="text-center">
                      <p className="text-sm text-gray-600">Uptime</p>
                      <p className="font-semibold text-gray-900">{formatPercent(node.uptime ?? null)}</p>
                    </div>
                    <div className="text-center">
                      <p className="text-sm text-gray-600">Last Sync</p>
                      <p className="font-semibold text-gray-900">{formatDateTime(node.last_sync)}</p>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-center py-12 text-gray-500">
              <AlertCircle className="w-12 h-12 mx-auto mb-3 text-gray-400" />
              <p>No MCP nodes configured</p>
              <p className="text-sm mt-1">Connect your store to enable MCP</p>
            </div>
          )}
        </div>
      </div>

      {/* Platform onboarding & catalog status */}
      {(platformOnboarding || (platformSummaries && platformSummaries.length > 0)) && (
        <div className="bg-white rounded-lg shadow">
          <div className="px-6 py-4 border-b">
            <h2 className="text-lg font-semibold text-gray-900">Platform Catalog Status</h2>
            <p className="text-sm text-gray-600">
              High level view of your platform onboarding record and imported catalog (Amazon, Temu, etc.).
            </p>
          </div>
          <div className="p-6 grid grid-cols-1 md:grid-cols-2 gap-6 text-sm">
            <div>
              <h3 className="font-semibold text-gray-900 mb-2">Onboarding record</h3>
              {platformOnboarding ? (
                <div className="space-y-1 text-gray-700 text-sm">
                  <p>
                    <span className="font-medium">Onboarding ID:</span>{' '}
                    {platformOnboarding.onboarding_id}
                  </p>
                  <p>
                    <span className="font-medium">Status:</span>{' '}
                    {platformOnboarding.status || 'unknown'}
                  </p>
                  {platformOnboarding.platform_profile && (
                    <p className="text-xs text-gray-500">
                      Source type:{' '}
                      {platformOnboarding.platform_profile.source_type || 'not specified'}
                    </p>
                  )}
                </div>
              ) : (
                <p className="text-xs text-gray-500">
                  No platform onboarding record found for this merchant. You can create one from the
                  Platform Onboarding page.
                </p>
              )}
            </div>
            <div>
              <h3 className="font-semibold text-gray-900 mb-2">Platforms in catalog</h3>
              {platformSummaries && platformSummaries.length > 0 ? (
                <div className="overflow-x-auto text-xs">
                  <table className="min-w-full text-left">
                    <thead>
                      <tr className="border-b border-slate-200 bg-slate-50">
                        <th className="px-2 py-1 font-medium text-slate-700">Platform</th>
                        <th className="px-2 py-1 font-medium text-slate-700">Products</th>
                        <th className="px-2 py-1 font-medium text-slate-700">Last Sync</th>
                      </tr>
                    </thead>
                    <tbody>
                      {platformSummaries.map((p) => (
                        <tr key={p.platform} className="border-b border-slate-100">
                          <td className="px-2 py-1 text-slate-700 uppercase">{p.platform}</td>
                          <td className="px-2 py-1 text-slate-700">{p.product_count ?? 0}</td>
                          <td className="px-2 py-1 text-slate-700">
                            {formatDateTime(p.last_sync || null)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="text-xs text-gray-500">
                  No products imported yet via platform reports or sync jobs.
                </p>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Unified catalog (products_cache) */}
      <div className="bg-white rounded-lg shadow">
        <div className="px-6 py-4 border-b flex flex-col md:flex-row md:items-center md:justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">Unified Catalog (products_cache)</h2>
            <p className="text-sm text-gray-600">
              Amazon/Temu reports + Shopify/Wix cache in one view. Data comes from the platform import worker.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <select
              value={catalogFilter}
              onChange={(e) => {
                setCatalogFilter(e.target.value as any);
                setCatalogPage(1);
              }}
              className="px-3 py-2 border rounded-lg text-sm"
            >
              <option value="all">All platforms</option>
              <option value="amazon">Amazon</option>
              <option value="temu">Temu</option>
              <option value="shopify">Shopify</option>
              <option value="wix">Wix</option>
            </select>
            <input
              type="text"
              value={catalogSearch}
              onChange={(e) => {
                setCatalogSearch(e.target.value);
                setCatalogPage(1);
              }}
              placeholder="Search by title or ID"
              className="px-3 py-2 border rounded-lg text-sm"
            />
            <button
              onClick={() => loadCatalogProducts(catalogFilter, catalogPage)}
              className="flex items-center space-x-2 px-4 py-2 border rounded-lg hover:bg-gray-50"
            >
              <RefreshCw className="w-4 h-4" />
              <span>Refresh</span>
            </button>
          </div>
        </div>
        <div className="p-6">
          {catalogLoading ? (
            <div className="py-10 text-center text-gray-500">Loading catalog…</div>
          ) : filteredCatalog.length === 0 ? (
            <div className="py-10 text-center text-gray-500">
              {catalogSearch ? 'No products match your search.' : 'No products found in products_cache.'}
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="min-w-full text-left text-sm">
                <thead>
                  <tr className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-600">
                    <th className="px-3 py-2">ID</th>
                    <th className="px-3 py-2">Title</th>
                    <th className="px-3 py-2">Platform</th>
                    <th className="px-3 py-2">Price</th>
                    <th className="px-3 py-2">Orderable</th>
                    <th className="px-3 py-2">Cached at</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredCatalog.map((p) => {
                    const isOrderable =
                      p.orderable === true || p.orderable_validation?.orderable === true;
                    const errors = p.orderable_validation?.errors || [];
                    return (
                      <tr key={`${p.platform}-${p.id}`} className="border-b border-slate-100">
                        <td className="px-3 py-2 font-mono text-xs text-slate-700 truncate max-w-[160px]">
                          {p.id}
                        </td>
                        <td className="px-3 py-2 text-slate-800 truncate max-w-[240px]">
                          {p.title || '(no title)'}
                        </td>
                        <td className="px-3 py-2 text-slate-700 uppercase text-xs">
                          {p.platform}
                        </td>
                        <td className="px-3 py-2 text-slate-800 text-sm">
                          {p.price} {p.currency}
                        </td>
                        <td className="px-3 py-2">
                          {isOrderable ? (
                            <span className="inline-flex items-center px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 text-xs">
                              Ready
                            </span>
                          ) : (
                            <span className="inline-flex items-center px-2 py-0.5 rounded-full bg-amber-50 text-amber-700 text-xs">
                              Needs data
                            </span>
                          )}
                          {!isOrderable && errors.length > 0 && (
                            <div className="text-[11px] text-slate-500 mt-1">{errors.join(', ')}</div>
                          )}
                        </td>
                        <td className="px-3 py-2 text-slate-600 text-xs">
                          {p.cached_at ? formatDateTime(p.cached_at) : '—'}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          <div className="mt-4 flex items-center justify-between text-sm text-gray-600">
            <div>
              Showing {(catalogPage - 1) * catalogPageSize + 1}-
              {Math.min(catalogPage * catalogPageSize, catalogTotal || catalogPage * catalogPageSize)} of{' '}
              {catalogTotal || filteredCatalog.length}
            </div>
            <div className="flex items-center gap-2">
              <button
                disabled={catalogPage === 1}
                onClick={() => setCatalogPage((p) => Math.max(1, p - 1))}
                className="px-3 py-1 border rounded hover:bg-gray-50 disabled:opacity-50"
              >
                Prev
              </button>
              <span className="text-xs">
                Page {catalogPage} / {Math.max(1, Math.ceil((catalogTotal || filteredCatalog.length) / catalogPageSize))}
              </span>
              <button
                disabled={catalogPage * catalogPageSize >= (catalogTotal || filteredCatalog.length)}
                onClick={() => setCatalogPage((p) => p + 1)}
                className="px-3 py-1 border rounded hover:bg-gray-50 disabled:opacity-50"
              >
                Next
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* Last Sync Info */}
      {(mcpStatus?.last_sync || mcpStatus?.latest_sync) && (
        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
          <p className="text-sm text-blue-900">
            <strong>Last Sync:</strong> {formatDateTime(mcpStatus?.last_sync || mcpStatus?.latest_sync)}
          </p>
        </div>
      )}
    </div>
  );
}
