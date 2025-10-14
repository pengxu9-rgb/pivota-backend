import React, { useState, useEffect } from 'react';
import { useAuth } from './useAuth';
import { supabase } from './supabase_client';
import { toast } from 'sonner';

interface PendingUser {
  id: string;
  email: string;
  role: string;
  approved: boolean;
  created_at: string;
}

interface PSP {
  id: string;
  name: string;
  status: string;
  connection_health: string;
  api_response_time: number;
  enabled: boolean;
}

interface AnalyticsData {
  period_days: number;
  timestamp: string;
  system_metrics: {
    total_payments: number;
    successful_payments: number;
    failed_payments: number;
    success_rate: number;
    active_agents: number;
    total_agents: number;
    active_merchants: number;
    total_merchants: number;
  };
  psp_performance: Record<string, any>;
  routing_usage: Record<string, any>;
  kyb_metrics: {
    total_merchants: number;
    approved_rate: number;
  };
  admin_actions: {
    total_logs: number;
    recent_actions: number;
    actions_per_day: number;
  };
  data_source: string;
}

interface SystemLog {
  id: string;
  timestamp: string;
  level: string;
  action: string;
  message: string;
  details: Record<string, any>;
}

interface ApiKey {
  id: string;
  name: string;
  permissions: string[];
  created_at: string;
  last_used: string | null;
  usage_count: number;
  usage_rate: number;
  days_since_creation: number;
  enabled: boolean;
  created_by: string;
}

const EnhancedAdminDashboard: React.FC = () => {
  const { user } = useAuth();
  const [pendingUsers, setPendingUsers] = useState<PendingUser[]>([]);
  const [psps, setPsps] = useState<PSP[]>([]);
  const [analytics, setAnalytics] = useState<AnalyticsData | null>(null);
  const [systemLogs, setSystemLogs] = useState<SystemLog[]>([]);
  const [apiKeys, setApiKeys] = useState<ApiKey[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<'users' | 'psps' | 'analytics' | 'logs' | 'dev'>('users');

  useEffect(() => {
    fetchAllData();
  }, []);

  const fetchAllData = async () => {
    setLoading(true);
    try {
      await Promise.all([
        fetchPendingUsers(),
        fetchPSPs(),
        fetchAnalytics(),
        fetchSystemLogs(),
        fetchApiKeys()
      ]);
    } catch (error) {
      console.error('Error fetching data:', error);
      toast.error('Failed to load dashboard data');
    } finally {
      setLoading(false);
    }
  };

  const fetchPendingUsers = async () => {
    try {
      const { data, error } = await supabase
        .from('user_roles')
        .select(`
          user_id,
          role,
          approved,
          created_at,
          profiles!inner(email)
        `)
        .eq('approved', false);

      if (error) throw error;

      const users = data?.map(item => ({
        id: item.user_id,
        email: item.profiles.email,
        role: item.role,
        approved: item.approved,
        created_at: item.created_at
      })) || [];

      setPendingUsers(users);
    } catch (error) {
      console.error('Error fetching pending users:', error);
    }
  };

  const fetchPSPs = async () => {
    try {
      const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';
      const response = await fetch(`${API_URL}/psp-fix/status`);
      
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }
      
      const data = await response.json();
      
      if (data.psp) {
        const pspList = Object.entries(data.psp).map(([id, pspData]: [string, any]) => ({
          id,
          name: pspData.name || id,
          status: pspData.status || 'unknown',
          connection_health: pspData.connection_health || 'unknown',
          api_response_time: pspData.api_response_time || 0,
          enabled: pspData.enabled !== false
        }));
        setPsps(pspList);
      }
    } catch (error) {
      console.error('Error fetching PSPs:', error);
    }
  };

  const fetchAnalytics = async () => {
    try {
      const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';
      const response = await fetch(`${API_URL}/admin/analytics/overview?days=30`);
      
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }
      
      const data = await response.json();
      setAnalytics(data);
    } catch (error) {
      console.error('Error fetching analytics:', error);
    }
  };

  const fetchSystemLogs = async () => {
    try {
      const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';
      const response = await fetch(`${API_URL}/admin/logs?limit=50&hours=24`);
      
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }
      
      const data = await response.json();
      setSystemLogs(data.logs || []);
    } catch (error) {
      console.error('Error fetching system logs:', error);
    }
  };

  const fetchApiKeys = async () => {
    try {
      const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';
      const response = await fetch(`${API_URL}/admin/dev/api-keys`);
      
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }
      
      const data = await response.json();
      setApiKeys(data.api_keys || []);
    } catch (error) {
      console.error('Error fetching API keys:', error);
    }
  };

  const testPSP = async (pspId: string) => {
    try {
      const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';
      const response = await fetch(`${API_URL}/admin/psp/${pspId}/test`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
      });
      
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }
      
      const result = await response.json();
      
      if (result.success) {
        toast.success(`PSP ${pspId} test successful!`);
        fetchPSPs(); // Refresh PSP data
      } else {
        toast.error(`PSP ${pspId} test failed: ${result.error || 'Unknown error'}`);
      }
    } catch (error) {
      console.error('Error testing PSP:', error);
      toast.error(`PSP test failed: ${error instanceof Error ? error.message : 'Unknown error'}`);
    }
  };

  const handleApproval = async (userId: string, approved: boolean) => {
    try {
      const { error } = await supabase
        .from('user_roles')
        .update({ 
          approved,
          approved_at: new Date().toISOString()
        })
        .eq('user_id', userId);

      if (error) throw error;

      toast.success(`User ${approved ? 'approved' : 'rejected'} successfully`);
      fetchPendingUsers();
    } catch (error) {
      console.error('Error updating user approval:', error);
      toast.error('Failed to update user approval');
    }
  };

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-center">
          <div className="animate-spin rounded-full h-32 w-32 border-b-2 border-blue-600 mx-auto"></div>
          <p className="mt-4 text-gray-600">Loading enhanced admin dashboard...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <div className="max-w-7xl mx-auto py-6 sm:px-6 lg:px-8">
        <div className="px-4 py-6 sm:px-0">
          <div className="border-4 border-dashed border-gray-200 rounded-lg p-8">
            <h1 className="text-3xl font-bold text-gray-900 mb-8">
              Enhanced Admin Dashboard
            </h1>
            
            {/* Tab Navigation */}
            <div className="border-b border-gray-200 mb-6">
              <nav className="-mb-px flex space-x-8">
                {[
                  { id: 'users', label: 'User Management' },
                  { id: 'psps', label: 'PSP Management' },
                  { id: 'analytics', label: 'Analytics' },
                  { id: 'logs', label: 'System Logs' },
                  { id: 'dev', label: 'Developer Tools' }
                ].map((tab) => (
                  <button
                    key={tab.id}
                    onClick={() => setActiveTab(tab.id as any)}
                    className={`py-2 px-1 border-b-2 font-medium text-sm ${
                      activeTab === tab.id
                        ? 'border-blue-500 text-blue-600'
                        : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
                    }`}
                  >
                    {tab.label}
                  </button>
                ))}
              </nav>
            </div>

            {/* User Management Tab */}
            {activeTab === 'users' && (
              <div className="bg-white shadow overflow-hidden sm:rounded-md">
                <div className="px-4 py-5 sm:px-6">
                  <h3 className="text-lg leading-6 font-medium text-gray-900">
                    Pending User Approvals
                  </h3>
                  <p className="mt-1 max-w-2xl text-sm text-gray-500">
                    Review and approve new user registrations
                  </p>
                </div>
                
                {pendingUsers.length === 0 ? (
                  <div className="px-4 py-5 sm:px-6">
                    <p className="text-gray-500">No pending approvals</p>
                  </div>
                ) : (
                  <ul className="divide-y divide-gray-200">
                    {pendingUsers.map((pendingUser) => (
                      <li key={pendingUser.id} className="px-4 py-4 sm:px-6">
                        <div className="flex items-center justify-between">
                          <div className="flex-1 min-w-0">
                            <p className="text-sm font-medium text-gray-900 truncate">
                              {pendingUser.email}
                            </p>
                            <p className="text-sm text-gray-500">
                              Role: {pendingUser.role} | 
                              Created: {new Date(pendingUser.created_at).toLocaleDateString()}
                            </p>
                          </div>
                          <div className="flex items-center space-x-2">
                            <button
                              onClick={() => handleApproval(pendingUser.id, true)}
                              className="inline-flex items-center px-3 py-1 border border-transparent text-sm leading-4 font-medium rounded-md text-white bg-green-600 hover:bg-green-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-green-500"
                            >
                              Approve
                            </button>
                            <button
                              onClick={() => handleApproval(pendingUser.id, false)}
                              className="inline-flex items-center px-3 py-1 border border-transparent text-sm leading-4 font-medium rounded-md text-white bg-red-600 hover:bg-red-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-red-500"
                            >
                              Reject
                            </button>
                          </div>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}

            {/* PSP Management Tab */}
            {activeTab === 'psps' && (
              <div className="bg-white shadow overflow-hidden sm:rounded-md">
                <div className="px-4 py-5 sm:px-6">
                  <h3 className="text-lg leading-6 font-medium text-gray-900">
                    Payment Service Providers
                  </h3>
                  <p className="mt-1 max-w-2xl text-sm text-gray-500">
                    Manage and test PSP connections
                  </p>
                </div>
                
                {psps.length === 0 ? (
                  <div className="px-4 py-5 sm:px-6">
                    <p className="text-gray-500">No PSPs configured</p>
                  </div>
                ) : (
                  <ul className="divide-y divide-gray-200">
                    {psps.map((psp) => (
                      <li key={psp.id} className="px-4 py-4 sm:px-6">
                        <div className="flex items-center justify-between">
                          <div className="flex-1 min-w-0">
                            <p className="text-sm font-medium text-gray-900 truncate">
                              {psp.name}
                            </p>
                            <p className="text-sm text-gray-500">
                              Status: {psp.status} | 
                              Health: {psp.connection_health} | 
                              Response Time: {psp.api_response_time}ms
                            </p>
                          </div>
                          <div className="flex items-center space-x-2">
                            <button
                              onClick={() => testPSP(psp.id)}
                              className="inline-flex items-center px-3 py-1 border border-transparent text-sm leading-4 font-medium rounded-md text-white bg-blue-600 hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500"
                            >
                              Test
                            </button>
                          </div>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}

            {/* Analytics Tab */}
            {activeTab === 'analytics' && (
              <div className="bg-white shadow overflow-hidden sm:rounded-md">
                <div className="px-4 py-5 sm:px-6">
                  <h3 className="text-lg leading-6 font-medium text-gray-900">
                    System Analytics
                  </h3>
                  <p className="mt-1 max-w-2xl text-sm text-gray-500">
                    Real-time system performance and metrics
                  </p>
                </div>
                
                {analytics ? (
                  <div className="px-4 py-5 sm:px-6">
                    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
                      <div className="bg-blue-50 p-4 rounded-lg">
                        <h4 className="text-sm font-medium text-blue-900">Total Payments</h4>
                        <p className="text-2xl font-bold text-blue-600">{analytics.system_metrics.total_payments}</p>
                      </div>
                      <div className="bg-green-50 p-4 rounded-lg">
                        <h4 className="text-sm font-medium text-green-900">Success Rate</h4>
                        <p className="text-2xl font-bold text-green-600">{analytics.system_metrics.success_rate}%</p>
                      </div>
                      <div className="bg-purple-50 p-4 rounded-lg">
                        <h4 className="text-sm font-medium text-purple-900">Active Agents</h4>
                        <p className="text-2xl font-bold text-purple-600">{analytics.system_metrics.active_agents}</p>
                      </div>
                      <div className="bg-orange-50 p-4 rounded-lg">
                        <h4 className="text-sm font-medium text-orange-900">Active Merchants</h4>
                        <p className="text-2xl font-bold text-orange-600">{analytics.system_metrics.active_merchants}</p>
                      </div>
                    </div>
                    
                    <div className="text-sm text-gray-500">
                      Data Source: {analytics.data_source} | Last Updated: {new Date(analytics.timestamp).toLocaleString()}
                    </div>
                  </div>
                ) : (
                  <div className="px-4 py-5 sm:px-6">
                    <p className="text-gray-500">Loading analytics data...</p>
                  </div>
                )}
              </div>
            )}

            {/* System Logs Tab */}
            {activeTab === 'logs' && (
              <div className="bg-white shadow overflow-hidden sm:rounded-md">
                <div className="px-4 py-5 sm:px-6">
                  <h3 className="text-lg leading-6 font-medium text-gray-900">
                    System Logs
                  </h3>
                  <p className="mt-1 max-w-2xl text-sm text-gray-500">
                    Real-time system events and monitoring
                  </p>
                </div>
                
                {systemLogs.length === 0 ? (
                  <div className="px-4 py-5 sm:px-6">
                    <p className="text-gray-500">No system logs available</p>
                  </div>
                ) : (
                  <ul className="divide-y divide-gray-200">
                    {systemLogs.slice(0, 20).map((log) => (
                      <li key={log.id} className="px-4 py-4 sm:px-6">
                        <div className="flex items-center justify-between">
                          <div className="flex-1 min-w-0">
                            <p className="text-sm font-medium text-gray-900 truncate">
                              {log.message}
                            </p>
                            <p className="text-sm text-gray-500">
                              {log.action} | {log.level} | {new Date(log.timestamp).toLocaleString()}
                            </p>
                          </div>
                          <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                            log.level === 'ERROR' ? 'bg-red-100 text-red-800' :
                            log.level === 'WARN' ? 'bg-yellow-100 text-yellow-800' :
                            'bg-green-100 text-green-800'
                          }`}>
                            {log.level}
                          </span>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}

            {/* Developer Tools Tab */}
            {activeTab === 'dev' && (
              <div className="bg-white shadow overflow-hidden sm:rounded-md">
                <div className="px-4 py-5 sm:px-6">
                  <h3 className="text-lg leading-6 font-medium text-gray-900">
                    Developer Tools
                  </h3>
                  <p className="mt-1 max-w-2xl text-sm text-gray-500">
                    API key management and development tools
                  </p>
                </div>
                
                {apiKeys.length === 0 ? (
                  <div className="px-4 py-5 sm:px-6">
                    <p className="text-gray-500">No API keys configured</p>
                  </div>
                ) : (
                  <ul className="divide-y divide-gray-200">
                    {apiKeys.map((key) => (
                      <li key={key.id} className="px-4 py-4 sm:px-6">
                        <div className="flex items-center justify-between">
                          <div className="flex-1 min-w-0">
                            <p className="text-sm font-medium text-gray-900 truncate">
                              {key.name}
                            </p>
                            <p className="text-sm text-gray-500">
                              Usage: {key.usage_count} times | Rate: {key.usage_rate}/day | 
                              Created: {new Date(key.created_at).toLocaleDateString()}
                            </p>
                          </div>
                          <div className="flex items-center space-x-2">
                            <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                              key.enabled ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'
                            }`}>
                              {key.enabled ? 'Active' : 'Inactive'}
                            </span>
                          </div>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

export default EnhancedAdminDashboard;
