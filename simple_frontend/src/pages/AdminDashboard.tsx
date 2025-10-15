import React, { useState, useEffect } from 'react';
import { adminApi, merchantApi } from '../services/api';
import { PSP, RoutingRule, Merchant, SystemLog, ApiKey, Analytics } from '../types';
import { 
  CreditCard, 
  Users, 
  BarChart3, 
  Settings, 
  Activity,
  AlertCircle,
  CheckCircle,
  Clock,
  TrendingUp
} from 'lucide-react';
import { MerchantOnboardingModal, MerchantFormData } from '../components/MerchantOnboardingModal';
import { KYBReviewModal } from '../components/KYBReviewModal';
import { MerchantDetailsModal } from '../components/MerchantDetailsModal';

export const AdminDashboard: React.FC = () => {
  const [activeTab, setActiveTab] = useState('overview');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  // Data states
  const [dashboard, setDashboard] = useState<any>(null);
  const [psps, setPsps] = useState<Record<string, PSP>>({});
  const [routingRules, setRoutingRules] = useState<RoutingRule[]>([]);
  const [merchants, setMerchants] = useState<Record<string, Merchant>>({});
  const [logs, setLogs] = useState<SystemLog[]>([]);
  const [apiKeys, setApiKeys] = useState<ApiKey[]>([]);
  const [analytics, setAnalytics] = useState<Analytics | null>(null);

  // Modal states
  const [showOnboardingModal, setShowOnboardingModal] = useState(false);
  const [showKYBModal, setShowKYBModal] = useState(false);
  const [showDetailsModal, setShowDetailsModal] = useState(false);
  const [selectedMerchant, setSelectedMerchant] = useState<any>(null);

  console.log('📊 AdminDashboard rendered', {
    loading,
    error,
    activeTab,
    showOnboardingModal,
    showKYBModal,
    showDetailsModal
  });

  console.log('🔍 Will render test modal?', showOnboardingModal && 'YES, should render red modal');

  useEffect(() => {
    console.log('📊 AdminDashboard useEffect - loading data');
    loadDashboardData();
  }, []);

  useEffect(() => {
    console.log('🎭 showOnboardingModal changed to:', showOnboardingModal);
  }, [showOnboardingModal]);

  const loadDashboardData = async () => {
    try {
      console.log('📊 Starting to load dashboard data...');
      setLoading(true);
      setError('');

      // Load dashboard overview
      console.log('📊 Loading dashboard overview...');
      const dashboardData = await adminApi.getDashboard();
      console.log('✅ Dashboard data:', dashboardData);
      setDashboard(dashboardData);

      // Load PSP data
      console.log('📊 Loading PSP data...');
      const pspData = await adminApi.getPSPStatus();
      console.log('✅ PSP data:', pspData);
      setPsps(pspData.psp);

      // Load routing rules
      console.log('📊 Loading routing rules...');
      const rulesData = await adminApi.getRoutingRules();
      console.log('✅ Routing rules:', rulesData);
      setRoutingRules(rulesData.rules);

      // Load merchants
      const merchantData = await adminApi.getMerchantKYB();
      setMerchants(merchantData.merchants);

      // Load system logs
      const logsData = await adminApi.getSystemLogs(20, 24);
      setLogs(logsData.logs);

      // Load API keys
      const keysData = await adminApi.getApiKeys();
      setApiKeys(keysData.api_keys);

      // Load analytics
      const analyticsData = await adminApi.getAnalytics(30);
      setAnalytics(analyticsData);

    } catch (err: any) {
      const errorMsg = err.response?.data?.detail || err.message || 'Failed to load dashboard data';
      console.error('❌ Dashboard load error:', err);
      console.error('❌ Error details:', {
        message: err.message,
        response: err.response?.data,
        status: err.response?.status
      });
      setError(errorMsg);
    } finally {
      console.log('📊 Dashboard loading complete. Loading:', false);
      setLoading(false);
    }
  };

  const handleTestPSP = async (pspId: string) => {
    try {
      console.log(`🧪 Testing PSP: ${pspId}`);
      const result = await adminApi.testPSP(pspId);
      console.log('✅ PSP test result:', result);
      
      // Show success message
      alert(`✅ PSP ${pspId} test successful!\n${result.message || 'Connection OK'}`);
      
      // Reload PSP data after test
      const pspData = await adminApi.getPSPStatus();
      setPsps(pspData.psp);
    } catch (err: any) {
      console.error('❌ PSP test failed:', err);
      const errorMsg = err.response?.data?.detail || err.message || 'PSP test failed';
      setError(errorMsg);
      alert(`❌ PSP test failed: ${errorMsg}`);
    }
  };

  const handleTogglePSP = async (pspId: string, enabled: boolean) => {
    try {
      await adminApi.togglePSP(pspId, enabled);
      // Reload PSP data after toggle
      const pspData = await adminApi.getPSPStatus();
      setPsps(pspData.psp);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to toggle PSP');
    }
  };

  // Merchant handlers
  const handleMerchantOnboard = async (merchantData: MerchantFormData) => {
    try {
      console.log('🏪 Onboarding merchant:', merchantData);
      const result = await merchantApi.onboard(merchantData);
      console.log('✅ Merchant onboarded:', result);
      
      // Show success message with merchant ID and next steps
      alert(`✅ Merchant "${merchantData.business_name}" onboarded successfully!\n\nMerchant ID: ${result.merchant_id}\n\nNext steps:\n1. Upload KYB documents\n2. Wait for admin approval\n\nYou can now upload documents for this merchant.`);
      
      // Reload merchants list
      await loadDashboardData();
    } catch (err: any) {
      console.error('❌ Merchant onboarding failed:', err);
      const errorMsg = err.response?.data?.detail || err.message || 'Failed to onboard merchant';
      throw new Error(errorMsg);
    }
  };

  const handleApproveMerchant = async (merchantId: string) => {
    try {
      console.log('✅ Approving merchant:', merchantId);
      const result = await merchantApi.approve(Number(merchantId));
      console.log('Merchant approved:', result);
      alert(`✅ ${result.message}`);
      await loadDashboardData();
    } catch (err: any) {
      console.error('❌ Failed to approve merchant:', err);
      const errorMsg = err.response?.data?.detail || err.message || 'Failed to approve merchant';
      throw new Error(errorMsg);
    }
  };

  const handleRejectMerchant = async (merchantId: string, reason: string) => {
    try {
      console.log('❌ Rejecting merchant:', merchantId, 'Reason:', reason);
      const result = await merchantApi.reject(Number(merchantId), reason);
      console.log('Merchant rejected:', result);
      alert(`❌ ${result.message}\nReason: ${reason}`);
      await loadDashboardData();
    } catch (err: any) {
      console.error('❌ Failed to reject merchant:', err);
      const errorMsg = err.response?.data?.detail || err.message || 'Failed to reject merchant';
      throw new Error(errorMsg);
    }
  };

  const tabs = [
    { id: 'overview', label: 'Overview', icon: BarChart3 },
    { id: 'psp', label: 'PSP Management', icon: CreditCard },
    { id: 'routing', label: 'Routing Rules', icon: Settings },
    { id: 'merchants', label: 'Merchants', icon: Users },
    { id: 'logs', label: 'System Logs', icon: Activity },
    { id: 'analytics', label: 'Analytics', icon: TrendingUp },
  ];

  if (loading) {
    return (
      <div className="flex justify-center items-center min-h-96">
        <div className="text-lg">Loading dashboard...</div>
      </div>
    );
  }

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-3xl font-bold text-gray-900 mb-2">Admin Dashboard</h1>
        <p className="text-gray-600">Manage your payment infrastructure</p>
      </div>

      {error && (
        <div className="bg-red-100 border border-red-400 text-red-700 px-4 py-3 rounded mb-6">
          <strong>Error:</strong> {typeof error === 'string' ? error : JSON.stringify(error)}
        </div>
      )}

      {/* Tab Navigation */}
      <div className="border-b border-gray-200 mb-6">
        <nav className="-mb-px flex space-x-8">
          {tabs.map((tab) => {
            const Icon = tab.icon;
            return (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`py-2 px-1 border-b-2 font-medium text-sm flex items-center gap-2 ${
                  activeTab === tab.id
                    ? 'border-blue-500 text-blue-600'
                    : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
                }`}
              >
                <Icon size={16} />
                {tab.label}
              </button>
            );
          })}
        </nav>
      </div>

      {/* Tab Content */}
      {activeTab === 'overview' && (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
          {/* System Health */}
          <div className="card">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-gray-600">System Health</p>
                <p className="text-2xl font-bold text-green-600">99.9%</p>
              </div>
              <CheckCircle className="text-green-500" size={24} />
            </div>
          </div>

          {/* Active PSPs */}
          <div className="card">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-gray-600">Active PSPs</p>
                <p className="text-2xl font-bold text-blue-600">
                  {dashboard?.psp_management?.active_psps || 0}
                </p>
              </div>
              <CreditCard className="text-blue-500" size={24} />
            </div>
          </div>

          {/* Total Transactions */}
          <div className="card">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-gray-600">Total Transactions</p>
                <p className="text-2xl font-bold text-purple-600">
                  {analytics?.system_metrics?.total_payments?.toLocaleString() || 0}
                </p>
              </div>
              <TrendingUp className="text-purple-500" size={24} />
            </div>
          </div>

          {/* Success Rate */}
          <div className="card">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-gray-600">Success Rate</p>
                <p className="text-2xl font-bold text-green-600">
                  {analytics?.system_metrics?.success_rate?.toFixed(1) || 0}%
                </p>
              </div>
              <CheckCircle className="text-green-500" size={24} />
            </div>
          </div>

          {/* Recent Activity */}
          <div className="card md:col-span-2 lg:col-span-4">
            <h3 className="text-lg font-semibold mb-4">Recent Activity</h3>
            <div className="space-y-3">
              {logs.slice(0, 5).map((log) => (
                <div key={log.id} className="flex items-center gap-3 p-3 bg-gray-50 rounded">
                  <div className={`w-2 h-2 rounded-full ${
                    log.level === 'INFO' ? 'bg-blue-500' :
                    log.level === 'WARN' ? 'bg-yellow-500' :
                    log.level === 'SUCCESS' ? 'bg-green-500' : 'bg-red-500'
                  }`} />
                  <div className="flex-1">
                    <p className="text-sm font-medium">{log.message}</p>
                    <p className="text-xs text-gray-500">
                      {new Date(log.timestamp).toLocaleString()}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {activeTab === 'psp' && (
        <div className="space-y-6">
          <div className="flex justify-between items-center">
            <h2 className="text-xl font-semibold">PSP Management</h2>
            <button
              onClick={loadDashboardData}
              className="btn btn-primary"
            >
              Refresh
            </button>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {Object.entries(psps).map(([pspId, psp]) => (
              <div key={pspId} className="card">
                <div className="flex justify-between items-start mb-4">
                  <div>
                    <h3 className="text-lg font-semibold">{psp.name}</h3>
                    <p className="text-sm text-gray-600 capitalize">{psp.type}</p>
                  </div>
                  <div className={`px-2 py-1 rounded text-xs font-medium ${
                    psp.status === 'active' ? 'bg-green-100 text-green-800' :
                    'bg-red-100 text-red-800'
                  }`}>
                    {psp.status}
                  </div>
                </div>

                <div className="space-y-2 mb-4">
                  <div className="flex justify-between text-sm">
                    <span>Connection Health:</span>
                    <span className={`font-medium ${
                      psp.connection_health === 'healthy' ? 'text-green-600' : 'text-red-600'
                    }`}>
                      {psp.connection_health}
                    </span>
                  </div>
                  <div className="flex justify-between text-sm">
                    <span>Response Time:</span>
                    <span>{psp.api_response_time}ms</span>
                  </div>
                  <div className="flex justify-between text-sm">
                    <span>Last Tested:</span>
                    <span>{new Date(psp.last_tested).toLocaleString()}</span>
                  </div>
                  <div className="flex justify-between text-sm">
                    <span>Test Result:</span>
                    <span className={`font-medium ${
                      psp.test_results?.success ? 'text-green-600' : 'text-red-600'
                    }`}>
                      {psp.test_results?.success ? 'Success' : 'Failed'}
                    </span>
                  </div>
                </div>

                <div className="flex gap-2">
                  <button
                    onClick={() => {
                      console.log('🔘 Test button clicked for PSP:', pspId);
                      handleTestPSP(pspId);
                    }}
                    className="btn btn-primary btn-sm flex-1"
                  >
                    Test Connection
                  </button>
                  <button
                    onClick={() => {
                      console.log('🔘 Toggle button clicked for PSP:', pspId, 'New state:', !psp.enabled);
                      handleTogglePSP(pspId, !psp.enabled);
                    }}
                    className={`btn btn-sm flex-1 ${
                      psp.enabled ? 'btn-danger' : 'btn-success'
                    }`}
                  >
                    {psp.enabled ? 'Disable' : 'Enable'}
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {activeTab === 'routing' && (
        <div className="space-y-6">
          <div className="flex justify-between items-center">
            <h2 className="text-xl font-semibold">Routing Rules</h2>
            <button className="btn btn-primary">
              Add Rule
            </button>
          </div>

          <div className="card">
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-gray-200">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                      Rule
                    </th>
                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                      Type
                    </th>
                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                      Target PSP
                    </th>
                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                      Status
                    </th>
                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                      Performance
                    </th>
                  </tr>
                </thead>
                <tbody className="bg-white divide-y divide-gray-200">
                  {routingRules.map((rule) => (
                    <tr key={rule.id}>
                      <td className="px-6 py-4 whitespace-nowrap">
                        <div className="text-sm font-medium text-gray-900">{rule.name}</div>
                        <div className="text-sm text-gray-500">Priority: {rule.priority}</div>
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-900 capitalize">
                        {rule.rule_type.replace('_', ' ')}
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-900">
                        {rule.target_psp}
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap">
                        <span className={`px-2 py-1 text-xs font-medium rounded-full ${
                          rule.enabled ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'
                        }`}>
                          {rule.enabled ? 'Active' : 'Inactive'}
                        </span>
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-900">
                        {rule.performance ? (
                          <div>
                            <div>Success: {(rule.performance.success_rate * 100).toFixed(1)}%</div>
                            <div>Avg: {rule.performance.avg_latency}ms</div>
                          </div>
                        ) : (
                          'No data'
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {activeTab === 'merchants' && (
        <div className="space-y-6">
          <div className="flex justify-between items-center">
            <h2 className="text-xl font-semibold">Merchant Management</h2>
            <button 
              onClick={() => {
                console.log('🔘 Onboard Merchant button clicked');
                console.log('Current showOnboardingModal state:', showOnboardingModal);
                setShowOnboardingModal(true);
                console.log('Set showOnboardingModal to true');
              }}
              className="btn btn-primary"
            >
              Onboard Merchant
            </button>
          </div>

          {Object.keys(merchants).length === 0 ? (
            <div className="card text-center py-12">
              <h3 className="text-lg font-semibold mb-2">No Merchants Configured</h3>
              <p className="text-gray-600 mb-4">
                Configure Shopify or Wix stores in Render environment variables:
              </p>
              <ul className="text-sm text-gray-500 space-y-1">
                <li>SHOPIFY_ACCESS_TOKEN + SHOPIFY_STORE_URL</li>
                <li>WIX_API_KEY + WIX_STORE_URL</li>
              </ul>
              <button 
                onClick={() => window.open('https://dashboard.render.com', '_blank')}
                className="btn btn-primary mt-4"
              >
                Configure in Render
              </button>
            </div>
          ) : (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              {Object.entries(merchants).map(([merchantId, merchant]) => (
              <div key={merchantId} className="card">
                <div className="flex justify-between items-start mb-4">
                  <div>
                    <h3 className="text-lg font-semibold">{merchant.name}</h3>
                    <p className="text-sm text-gray-600 capitalize">{merchant.platform}</p>
                    <a 
                      href={merchant.store_url} 
                      target="_blank" 
                      rel="noopener noreferrer"
                      className="text-sm text-blue-600 hover:underline"
                    >
                      {merchant.store_url}
                    </a>
                  </div>
                  <div className={`px-2 py-1 rounded text-xs font-medium ${
                    merchant.status === 'approved' ? 'bg-green-100 text-green-800' :
                    merchant.status === 'in_progress' ? 'bg-yellow-100 text-yellow-800' :
                    'bg-red-100 text-red-800'
                  }`}>
                    {merchant.status}
                  </div>
                </div>

                <div className="space-y-2 mb-4">
                  <div className="flex justify-between text-sm">
                    <span>Verification:</span>
                    <span className="capitalize">{merchant.verification_status}</span>
                  </div>
                  <div className="flex justify-between text-sm">
                    <span>Volume Processed:</span>
                    <span>€{merchant.volume_processed?.toLocaleString()}</span>
                  </div>
                  <div className="flex justify-between text-sm">
                    <span>Documents:</span>
                    <span>{merchant.kyb_documents?.length || 0} uploaded</span>
                  </div>
                  <div className="flex justify-between text-sm">
                    <span>Last Activity:</span>
                    <span>{new Date(merchant.last_activity).toLocaleDateString()}</span>
                  </div>
                </div>

                <div className="flex gap-2">
                  <button 
                    onClick={() => {
                      setSelectedMerchant(merchant);
                      setShowKYBModal(true);
                    }}
                    className="btn btn-primary btn-sm flex-1"
                  >
                    Review KYB
                  </button>
                  <button 
                    onClick={() => {
                      setSelectedMerchant(merchant);
                      setShowDetailsModal(true);
                    }}
                    className="btn btn-secondary btn-sm flex-1"
                  >
                    View Details
                  </button>
                </div>
              </div>
              ))}
            </div>
          )}
        </div>
      )}

      {activeTab === 'logs' && (
        <div className="space-y-6">
          <div className="flex justify-between items-center">
            <h2 className="text-xl font-semibold">System Logs</h2>
            <button onClick={loadDashboardData} className="btn btn-primary">
              Refresh
            </button>
          </div>

          <div className="card">
            <div className="space-y-4">
              {logs.map((log) => (
                <div key={log.id} className="border-b border-gray-200 pb-4 last:border-b-0">
                  <div className="flex items-start gap-3">
                    <div className={`w-3 h-3 rounded-full mt-1 ${
                      log.level === 'INFO' ? 'bg-blue-500' :
                      log.level === 'WARN' ? 'bg-yellow-500' :
                      log.level === 'SUCCESS' ? 'bg-green-500' : 'bg-red-500'
                    }`} />
                    <div className="flex-1">
                      <div className="flex items-center gap-2 mb-1">
                        <span className={`text-xs font-medium px-2 py-1 rounded ${
                          log.level === 'INFO' ? 'bg-blue-100 text-blue-800' :
                          log.level === 'WARN' ? 'bg-yellow-100 text-yellow-800' :
                          log.level === 'SUCCESS' ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'
                        }`}>
                          {log.level}
                        </span>
                        <span className="text-xs text-gray-500">{log.action}</span>
                        <span className="text-xs text-gray-500">
                          {new Date(log.timestamp).toLocaleString()}
                        </span>
                      </div>
                      <p className="text-sm text-gray-900 mb-2">{log.message}</p>
                      {log.details && Object.keys(log.details).length > 0 && (
                        <details className="text-xs text-gray-600">
                          <summary className="cursor-pointer">Details</summary>
                          <pre className="mt-2 p-2 bg-gray-100 rounded text-xs overflow-x-auto">
                            {JSON.stringify(log.details, null, 2)}
                          </pre>
                        </details>
                      )}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {activeTab === 'analytics' && analytics && (
        <div className="space-y-6">
          <div className="flex justify-between items-center">
            <h2 className="text-xl font-semibold">Analytics Overview</h2>
            <div className="text-sm text-gray-600">
              Last {analytics.period_days} days
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
            <div className="card">
              <h3 className="text-sm font-medium text-gray-600 mb-2">Total Payments</h3>
              <p className="text-2xl font-bold text-blue-600">
                {analytics.system_metrics.total_payments.toLocaleString()}
              </p>
            </div>
            <div className="card">
              <h3 className="text-sm font-medium text-gray-600 mb-2">Success Rate</h3>
              <p className="text-2xl font-bold text-green-600">
                {analytics.system_metrics.success_rate.toFixed(1)}%
              </p>
            </div>
            <div className="card">
              <h3 className="text-sm font-medium text-gray-600 mb-2">Active Agents</h3>
              <p className="text-2xl font-bold text-purple-600">
                {analytics.system_metrics.active_agents}
              </p>
            </div>
            <div className="card">
              <h3 className="text-sm font-medium text-gray-600 mb-2">Active Merchants</h3>
              <p className="text-2xl font-bold text-orange-600">
                {analytics.system_metrics.active_merchants}
              </p>
            </div>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <div className="card">
              <h3 className="text-lg font-semibold mb-4">PSP Performance</h3>
              <div className="space-y-3">
                {Object.entries(analytics.psp_performance).map(([pspId, performance]) => (
                  <div key={pspId} className="flex justify-between items-center p-3 bg-gray-50 rounded">
                    <span className="font-medium capitalize">{pspId}</span>
                    <div className="text-sm text-gray-600">
                      <div>Status: {performance.status}</div>
                      <div>Health: {performance.connection_health}</div>
                      <div>Response: {performance.api_response_time}ms</div>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div className="card">
              <h3 className="text-lg font-semibold mb-4">KYB Metrics</h3>
              <div className="space-y-3">
                <div className="flex justify-between">
                  <span>Total Merchants:</span>
                  <span className="font-medium">{analytics.kyb_metrics.total_merchants}</span>
                </div>
                <div className="flex justify-between">
                  <span>Approval Rate:</span>
                  <span className="font-medium">{analytics.kyb_metrics.approved_rate.toFixed(1)}%</span>
                </div>
                <div className="flex justify-between">
                  <span>Admin Actions:</span>
                  <span className="font-medium">{analytics.admin_actions.recent_actions}</span>
                </div>
                <div className="flex justify-between">
                  <span>Actions/Day:</span>
                  <span className="font-medium">{analytics.admin_actions.actions_per_day}</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Modals - with CSS class backdrop */}
      <div 
        className={showOnboardingModal ? 'modal-backdrop' : 'modal-backdrop-hidden'}
        onClick={() => setShowOnboardingModal(false)}
      >
        <MerchantOnboardingModal
          isOpen={showOnboardingModal}
          onClose={() => setShowOnboardingModal(false)}
          onSubmit={handleMerchantOnboard}
        />
      </div>

      <div 
        className={showKYBModal ? 'modal-backdrop' : 'modal-backdrop-hidden'}
        onClick={() => {
          setShowKYBModal(false);
          setSelectedMerchant(null);
        }}
      >
        <KYBReviewModal
          isOpen={showKYBModal}
          onClose={() => {
            setShowKYBModal(false);
            setSelectedMerchant(null);
          }}
          merchant={selectedMerchant}
          onApprove={handleApproveMerchant}
          onReject={handleRejectMerchant}
        />
      </div>

      <div 
        className={showDetailsModal ? 'modal-backdrop' : 'modal-backdrop-hidden'}
        onClick={() => {
          setShowDetailsModal(false);
          setSelectedMerchant(null);
        }}
      >
        <MerchantDetailsModal
          isOpen={showDetailsModal}
          onClose={() => {
            setShowDetailsModal(false);
            setSelectedMerchant(null);
          }}
          merchant={selectedMerchant}
        />
      </div>
    </div>
  );
};


