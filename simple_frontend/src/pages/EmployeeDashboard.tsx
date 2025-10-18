import React, { useState, useEffect } from 'react';
import { useAuth } from '../contexts/AuthContext';
import { adminApi, merchantApi, agentApi } from '../services/api';
import { 
  Users, 
  Store, 
  Bot,
  BarChart3, 
  Settings, 
  Activity,
  AlertCircle,
  CheckCircle,
  TrendingUp,
  Eye,
  FileCheck,
  Upload,
  MoreVertical,
  RefreshCw,
  Link,
  Boxes,
  Trash2
} from 'lucide-react';

export const EmployeeDashboard: React.FC = () => {
  const { user, signout } = useAuth();
  const [activeTab, setActiveTab] = useState('merchants');
  const [loading, setLoading] = useState(false);
  const [merchants, setMerchants] = useState<any[]>([]);
  const [agents, setAgents] = useState<any[]>([]);
  const [analytics, setAnalytics] = useState<any>(null);

  useEffect(() => {
    if (user?.role && !['employee', 'admin', 'super_admin'].includes(user.role)) {
      window.location.href = '/login';
    }
    loadData();
  }, [user]);

  const loadData = async () => {
    setLoading(true);
    try {
      // Load merchants
      const merchantsData = await merchantApi.listMerchants();
      setMerchants(merchantsData);

      // Load agents  
      const agentsData = await adminApi.getApiKeys();
      setAgents(agentsData.api_keys || []);

      // Load analytics
      const analyticsData = await adminApi.getAnalytics(30);
      setAnalytics(analyticsData);
    } catch (error) {
      console.error('Failed to load data:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleMerchantAction = async (action: string, merchant: any) => {
    switch(action) {
      case 'approve':
        if (window.confirm(`Approve merchant ${merchant.business_name}?`)) {
          await merchantApi.approve(merchant.merchant_id);
          await loadData();
        }
        break;
      case 'reject':
        const reason = window.prompt('Rejection reason:');
        if (reason) {
          await merchantApi.reject(merchant.merchant_id, reason);
          await loadData();
        }
        break;
      case 'delete':
        if (window.confirm(`Delete merchant ${merchant.business_name}?`)) {
          await merchantApi.deleteMerchant(merchant.merchant_id);
          await loadData();
        }
        break;
      case 'upload':
        // Open upload modal
        alert('Document upload feature coming soon');
        break;
      case 'connect':
        // Connect to Shopify/Wix
        alert('Store connection feature coming soon');
        break;
    }
  };

  const handleAgentAction = async (action: string, agent: any) => {
    switch(action) {
      case 'view':
        alert(`Agent Details:\n\nID: ${agent.agent_id}\nName: ${agent.name}\nType: ${agent.type}\nStatus: ${agent.is_active ? 'Active' : 'Inactive'}`);
        break;
      case 'reset':
        if (window.confirm(`Reset API key for agent ${agent.name}?`)) {
          // Reset API key
          alert('API key reset feature coming soon');
        }
        break;
      case 'deactivate':
        if (window.confirm(`Deactivate agent ${agent.name}?`)) {
          // Deactivate agent
          alert('Agent deactivation feature coming soon');
        }
        break;
    }
  };

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white shadow-sm border-b">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex justify-between items-center h-16">
            <div className="flex items-center">
              <h1 className="text-xl font-bold text-gray-900">Employee Portal</h1>
              <span className="ml-4 text-sm text-gray-500">
                Welcome, {user?.email} ({user?.role})
              </span>
            </div>
            <button
              onClick={signout}
              className="px-4 py-2 text-sm text-red-600 hover:text-red-800"
            >
              Sign Out
            </button>
          </div>
        </div>
      </header>

      {/* Navigation Tabs */}
      <div className="bg-white border-b">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <nav className="flex space-x-8">
            {[
              { id: 'merchants', label: 'Merchants', icon: Store },
              { id: 'agents', label: 'Agents', icon: Bot },
              { id: 'analytics', label: 'Analytics', icon: BarChart3 },
              { id: 'system', label: 'System', icon: Settings }
            ].map(tab => {
              const Icon = tab.icon;
              return (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id)}
                  className={`py-4 px-1 border-b-2 font-medium text-sm flex items-center gap-2 ${
                    activeTab === tab.id
                      ? 'border-purple-500 text-purple-600'
                      : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
                  }`}
                >
                  <Icon className="w-4 h-4" />
                  {tab.label}
                </button>
              );
            })}
          </nav>
        </div>
      </div>

      {/* Main Content */}
      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {loading ? (
          <div className="flex justify-center items-center h-64">
            <RefreshCw className="w-8 h-8 text-gray-400 animate-spin" />
          </div>
        ) : (
          <>
            {/* Merchants Tab */}
            {activeTab === 'merchants' && (
              <div>
                <div className="mb-6 flex justify-between items-center">
                  <h2 className="text-lg font-semibold">Merchant Management</h2>
                  <button
                    onClick={loadData}
                    className="px-4 py-2 bg-purple-600 text-white rounded-lg hover:bg-purple-700"
                  >
                    Refresh
                  </button>
                </div>

                <div className="bg-white shadow rounded-lg overflow-hidden">
                  <table className="min-w-full">
                    <thead className="bg-gray-50">
                      <tr>
                        <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                          Merchant
                        </th>
                        <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                          Status
                        </th>
                        <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                          PSP
                        </th>
                        <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                          Store
                        </th>
                        <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                          Created
                        </th>
                        <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                          Actions
                        </th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-200">
                      {merchants.map(merchant => (
                        <tr key={merchant.merchant_id}>
                          <td className="px-6 py-4">
                            <div>
                              <div className="text-sm font-medium text-gray-900">
                                {merchant.business_name}
                              </div>
                              <div className="text-sm text-gray-500">{merchant.merchant_id}</div>
                            </div>
                          </td>
                          <td className="px-6 py-4">
                            <span className={`inline-flex px-2 py-1 text-xs rounded-full ${
                              merchant.status === 'approved' 
                                ? 'bg-green-100 text-green-800'
                                : merchant.status === 'rejected'
                                ? 'bg-red-100 text-red-800'
                                : 'bg-yellow-100 text-yellow-800'
                            }`}>
                              {merchant.status}
                            </span>
                          </td>
                          <td className="px-6 py-4 text-sm text-gray-900">
                            {merchant.psp_connected ? '✅ Connected' : '⏳ Pending'}
                          </td>
                          <td className="px-6 py-4 text-sm text-gray-900">
                            {merchant.mcp_connected 
                              ? `✅ ${merchant.mcp_platform}`
                              : '⏳ Not connected'}
                          </td>
                          <td className="px-6 py-4 text-sm text-gray-500">
                            {new Date(merchant.created_at).toLocaleDateString()}
                          </td>
                          <td className="px-6 py-4">
                            <div className="relative inline-block text-left">
                              <details className="group">
                                <summary className="list-none cursor-pointer p-2 hover:bg-gray-100 rounded">
                                  <MoreVertical className="w-4 h-4 text-gray-500" />
                                </summary>
                                <div className="absolute right-0 mt-2 w-48 bg-white rounded-md shadow-lg z-10 border">
                                  {merchant.status === 'pending_verification' && (
                                    <>
                                      <button
                                        onClick={() => handleMerchantAction('approve', merchant)}
                                        className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100"
                                      >
                                        ✅ Approve
                                      </button>
                                      <button
                                        onClick={() => handleMerchantAction('reject', merchant)}
                                        className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100"
                                      >
                                        ❌ Reject
                                      </button>
                                    </>
                                  )}
                                  <button
                                    onClick={() => handleMerchantAction('upload', merchant)}
                                    className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100 flex items-center gap-2"
                                  >
                                    <Upload className="w-4 h-4" /> Upload Docs
                                  </button>
                                  {!merchant.mcp_connected && (
                                    <button
                                      onClick={() => handleMerchantAction('connect', merchant)}
                                      className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100 flex items-center gap-2"
                                    >
                                      <Link className="w-4 h-4" /> Connect Store
                                    </button>
                                  )}
                                  <button
                                    onClick={() => handleMerchantAction('delete', merchant)}
                                    className="w-full text-left px-4 py-2 text-sm text-red-600 hover:bg-gray-100 flex items-center gap-2"
                                  >
                                    <Trash2 className="w-4 h-4" /> Delete
                                  </button>
                                </div>
                              </details>
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {/* Agents Tab */}
            {activeTab === 'agents' && (
              <div>
                <div className="mb-6 flex justify-between items-center">
                  <h2 className="text-lg font-semibold">Agent Management</h2>
                  <button className="px-4 py-2 bg-purple-600 text-white rounded-lg hover:bg-purple-700">
                    + Create Agent
                  </button>
                </div>

                <div className="grid gap-4">
                  {agents.map(agent => (
                    <div key={agent.key_id} className="bg-white rounded-lg shadow p-6">
                      <div className="flex justify-between items-start">
                        <div>
                          <h3 className="text-lg font-medium">{agent.name || 'Unnamed Agent'}</h3>
                          <p className="text-sm text-gray-500">ID: {agent.key_id}</p>
                          <p className="text-sm text-gray-500">Type: {agent.type}</p>
                          <div className="mt-2">
                            <span className={`inline-flex px-2 py-1 text-xs rounded-full ${
                              agent.is_active 
                                ? 'bg-green-100 text-green-800'
                                : 'bg-gray-100 text-gray-800'
                            }`}>
                              {agent.is_active ? 'Active' : 'Inactive'}
                            </span>
                          </div>
                        </div>
                        <div className="relative">
                          <details className="group">
                            <summary className="list-none cursor-pointer p-2 hover:bg-gray-100 rounded">
                              <MoreVertical className="w-4 h-4 text-gray-500" />
                            </summary>
                            <div className="absolute right-0 mt-2 w-48 bg-white rounded-md shadow-lg z-10 border">
                              <button
                                onClick={() => handleAgentAction('view', agent)}
                                className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100 flex items-center gap-2"
                              >
                                <Eye className="w-4 h-4" /> View Details
                              </button>
                              <button
                                onClick={() => handleAgentAction('reset', agent)}
                                className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100"
                              >
                                🔑 Reset API Key
                              </button>
                              <button
                                onClick={() => handleAgentAction('deactivate', agent)}
                                className="w-full text-left px-4 py-2 text-sm text-red-600 hover:bg-gray-100"
                              >
                                ⛔ Deactivate
                              </button>
                            </div>
                          </details>
                        </div>
                      </div>
                      <div className="mt-4 grid grid-cols-3 gap-4 text-sm">
                        <div>
                          <span className="text-gray-500">Requests</span>
                          <p className="font-semibold">{agent.usage?.total_requests || 0}</p>
                        </div>
                        <div>
                          <span className="text-gray-500">Success Rate</span>
                          <p className="font-semibold">{agent.usage?.success_rate || 0}%</p>
                        </div>
                        <div>
                          <span className="text-gray-500">Last Used</span>
                          <p className="font-semibold">
                            {agent.last_used 
                              ? new Date(agent.last_used).toLocaleDateString()
                              : 'Never'}
                          </p>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Analytics Tab */}
            {activeTab === 'analytics' && (
              <div>
                <h2 className="text-lg font-semibold mb-6">Platform Analytics</h2>
                
                {/* Summary Cards */}
                <div className="grid grid-cols-4 gap-6 mb-8">
                  <div className="bg-white rounded-lg shadow p-6">
                    <div className="flex items-center justify-between">
                      <div>
                        <p className="text-sm text-gray-500">Total Merchants</p>
                        <p className="text-2xl font-bold">{merchants.length}</p>
                      </div>
                      <Store className="w-8 h-8 text-purple-500" />
                    </div>
                  </div>
                  <div className="bg-white rounded-lg shadow p-6">
                    <div className="flex items-center justify-between">
                      <div>
                        <p className="text-sm text-gray-500">Active Agents</p>
                        <p className="text-2xl font-bold">
                          {agents.filter(a => a.is_active).length}
                        </p>
                      </div>
                      <Bot className="w-8 h-8 text-green-500" />
                    </div>
                  </div>
                  <div className="bg-white rounded-lg shadow p-6">
                    <div className="flex items-center justify-between">
                      <div>
                        <p className="text-sm text-gray-500">Total GMV</p>
                        <p className="text-2xl font-bold">
                          ${analytics?.total_gmv?.toLocaleString() || '0'}
                        </p>
                      </div>
                      <TrendingUp className="w-8 h-8 text-blue-500" />
                    </div>
                  </div>
                  <div className="bg-white rounded-lg shadow p-6">
                    <div className="flex items-center justify-between">
                      <div>
                        <p className="text-sm text-gray-500">Success Rate</p>
                        <p className="text-2xl font-bold">
                          {analytics?.success_rate || '0'}%
                        </p>
                      </div>
                      <CheckCircle className="w-8 h-8 text-green-500" />
                    </div>
                  </div>
                </div>

                {/* Charts placeholder */}
                <div className="bg-white rounded-lg shadow p-6">
                  <h3 className="text-lg font-medium mb-4">Transaction Volume (Last 30 Days)</h3>
                  <div className="h-64 flex items-center justify-center text-gray-400">
                    <BarChart3 className="w-12 h-12" />
                    <span className="ml-4">Chart visualization coming soon</span>
                  </div>
                </div>
              </div>
            )}

            {/* System Tab */}
            {activeTab === 'system' && (
              <div>
                <h2 className="text-lg font-semibold mb-6">System Configuration</h2>
                
                <div className="grid gap-6">
                  <div className="bg-white rounded-lg shadow p-6">
                    <h3 className="text-md font-medium mb-4">Payment Service Providers</h3>
                    <div className="space-y-4">
                      <div className="flex justify-between items-center">
                        <div>
                          <p className="font-medium">Stripe</p>
                          <p className="text-sm text-gray-500">Connected and active</p>
                        </div>
                        <span className="px-3 py-1 bg-green-100 text-green-800 rounded-full text-sm">
                          Active
                        </span>
                      </div>
                      <div className="flex justify-between items-center">
                        <div>
                          <p className="font-medium">Adyen</p>
                          <p className="text-sm text-gray-500">Connected and active</p>
                        </div>
                        <span className="px-3 py-1 bg-green-100 text-green-800 rounded-full text-sm">
                          Active
                        </span>
                      </div>
                    </div>
                  </div>

                  <div className="bg-white rounded-lg shadow p-6">
                    <h3 className="text-md font-medium mb-4">System Health</h3>
                    <div className="space-y-3">
                      <div className="flex justify-between">
                        <span className="text-gray-600">API Status</span>
                        <span className="text-green-600">● Operational</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-gray-600">Database</span>
                        <span className="text-green-600">● Connected</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-gray-600">Webhook Service</span>
                        <span className="text-green-600">● Active</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-gray-600">MCP Server</span>
                        <span className="text-green-600">● Running</span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            )}
          </>
        )}
      </main>
    </div>
  );
};
