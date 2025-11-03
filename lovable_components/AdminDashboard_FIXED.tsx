import React, { useState, useEffect } from 'react';
import { toast } from 'sonner';

interface PendingUser {
  id: string;
  user_id: string;
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

interface Analytics {
  total_transactions: number;
  total_volume: number;
  success_rate: number;
  active_psps: number;
}

const AdminDashboard: React.FC = () => {
  const [pendingUsers, setPendingUsers] = useState<PendingUser[]>([]);
  const [psps, setPsps] = useState<PSP[]>([]);
  const [analytics, setAnalytics] = useState<Analytics | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<'users' | 'psps' | 'analytics'>('users');
  
  // Get API URL and token from environment/localStorage
  const API_URL = import.meta.env.VITE_API_URL || 'https://pivota-dashboard.onrender.com';
  const token = localStorage.getItem('auth_token');

  useEffect(() => {
    if (token) {
      fetchPendingUsers();
      fetchPSPs();
      fetchAnalytics();
    } else {
      setLoading(false);
      toast.error('Please sign in to access admin dashboard');
    }
  }, [token]);

  const fetchPendingUsers = async () => {
    try {
      const response = await fetch(`${API_URL}/auth/admin/users`, {
        headers: {
          'Authorization': `Bearer ${token}`,
        },
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const data = await response.json();
      setPendingUsers(data.users || []);
    } catch (error) {
      console.error('Error fetching pending users:', error);
      toast.error('Failed to load pending users');
    } finally {
      setLoading(false);
    }
  };

  const handleApproval = async (userId: string, approved: boolean) => {
    try {
      const response = await fetch(`${API_URL}/auth/admin/users/${userId}/approve`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ approved }),
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      toast.success(`User ${approved ? 'approved' : 'rejected'} successfully`);
      fetchPendingUsers();
    } catch (error) {
      console.error('Error updating user approval:', error);
      toast.error('Failed to update user approval');
    }
  };

  const handleRoleChange = async (userId: string, newRole: string) => {
    try {
      const response = await fetch(`${API_URL}/auth/admin/users/${userId}/role`, {
        method: 'PUT',
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ role: newRole }),
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      toast.success('User role updated successfully');
      fetchPendingUsers();
    } catch (error) {
      console.error('Error updating user role:', error);
      toast.error('Failed to update user role');
    }
  };

  const fetchPSPs = async () => {
    try {
      const response = await fetch(`${API_URL}/admin/psp/status`, {
        headers: {
          'Authorization': `Bearer ${token}`,
        },
      });

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
      toast.error('Failed to load PSP data');
    }
  };

  const fetchAnalytics = async () => {
    try {
      const response = await fetch(`${API_URL}/admin/analytics/overview`, {
        headers: {
          'Authorization': `Bearer ${token}`,
        },
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const data = await response.json();
      setAnalytics(data.analytics || null);
    } catch (error) {
      console.error('Error fetching analytics:', error);
      toast.error('Failed to load analytics');
    }
  };

  const testPSP = async (pspId: string) => {
    try {
      const response = await fetch(`${API_URL}/admin/psp/${pspId}/test`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const result = await response.json();

      if (result.test_results?.success) {
        toast.success(`PSP ${pspId} test successful! Latency: ${result.test_results.latency_ms}ms`);
        fetchPSPs();
      } else {
        toast.error(`PSP ${pspId} test failed: ${result.test_results?.error || 'Unknown error'}`);
      }
    } catch (error) {
      console.error('Error testing PSP:', error);
      toast.error(`PSP test failed: ${error instanceof Error ? error.message : 'Unknown error'}`);
    }
  };

  const togglePSP = async (pspId: string, enabled: boolean) => {
    try {
      const response = await fetch(`${API_URL}/admin/psp/${pspId}/toggle`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ enable: enabled }),
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const result = await response.json();

      if (result.status === 'success') {
        toast.success(`PSP ${pspId} ${enabled ? 'enabled' : 'disabled'} successfully`);
        fetchPSPs();
      } else {
        toast.error(`Failed to ${enabled ? 'enable' : 'disable'} PSP: ${result.message}`);
      }
    } catch (error) {
      console.error('Error toggling PSP:', error);
      toast.error(`Failed to toggle PSP: ${error instanceof Error ? error.message : 'Unknown error'}`);
    }
  };

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-center">
          <div className="animate-spin rounded-full h-32 w-32 border-b-2 border-blue-600 mx-auto"></div>
          <p className="mt-4 text-gray-600">Loading admin dashboard...</p>
        </div>
      </div>
    );
  }

  if (!token) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-center">
          <h2 className="text-2xl font-bold text-gray-900 mb-4">Access Denied</h2>
          <p className="text-gray-600">Please sign in to access the admin dashboard</p>
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
              Admin Dashboard
            </h1>

            {/* Tab Navigation */}
            <div className="border-b border-gray-200 mb-6">
              <nav className="-mb-px flex space-x-8">
                <button
                  onClick={() => setActiveTab('users')}
                  className={`py-2 px-1 border-b-2 font-medium text-sm ${
                    activeTab === 'users'
                      ? 'border-blue-500 text-blue-600'
                      : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
                  }`}
                >
                  User Management
                </button>
                <button
                  onClick={() => setActiveTab('psps')}
                  className={`py-2 px-1 border-b-2 font-medium text-sm ${
                    activeTab === 'psps'
                      ? 'border-blue-500 text-blue-600'
                      : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
                  }`}
                >
                  PSP Management
                </button>
                <button
                  onClick={() => setActiveTab('analytics')}
                  className={`py-2 px-1 border-b-2 font-medium text-sm ${
                    activeTab === 'analytics'
                      ? 'border-blue-500 text-blue-600'
                      : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
                  }`}
                >
                  Analytics
                </button>
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
                            <select
                              value={pendingUser.role}
                              onChange={(e) => handleRoleChange(pendingUser.user_id, e.target.value)}
                              className="text-sm border border-gray-300 rounded-md px-2 py-1"
                            >
                              <option value="employee">Employee</option>
                              <option value="agent">Agent</option>
                              <option value="merchant">Merchant</option>
                              <option value="operator">Operator</option>
                              <option value="admin">Admin</option>
                            </select>
                            <button
                              onClick={() => handleApproval(pendingUser.user_id, true)}
                              className="inline-flex items-center px-3 py-1 border border-transparent text-sm leading-4 font-medium rounded-md text-white bg-green-600 hover:bg-green-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-green-500"
                            >
                              Approve
                            </button>
                            <button
                              onClick={() => handleApproval(pendingUser.user_id, false)}
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
                            <button
                              onClick={() => togglePSP(psp.id, !psp.enabled)}
                              className={`inline-flex items-center px-3 py-1 border border-transparent text-sm leading-4 font-medium rounded-md ${
                                psp.enabled
                                  ? 'text-white bg-red-600 hover:bg-red-700 focus:ring-red-500'
                                  : 'text-white bg-green-600 hover:bg-green-700 focus:ring-green-500'
                              } focus:outline-none focus:ring-2 focus:ring-offset-2`}
                            >
                              {psp.enabled ? 'Disable' : 'Enable'}
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
                    Overview of system performance and metrics
                  </p>
                </div>

                {analytics ? (
                  <div className="px-4 py-5 sm:px-6 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
                    <div className="bg-blue-50 p-4 rounded-lg">
                      <p className="text-sm text-gray-600">Total Transactions</p>
                      <p className="text-2xl font-bold text-blue-600">{analytics.total_transactions}</p>
                    </div>
                    <div className="bg-green-50 p-4 rounded-lg">
                      <p className="text-sm text-gray-600">Total Volume</p>
                      <p className="text-2xl font-bold text-green-600">${analytics.total_volume.toLocaleString()}</p>
                    </div>
                    <div className="bg-purple-50 p-4 rounded-lg">
                      <p className="text-sm text-gray-600">Success Rate</p>
                      <p className="text-2xl font-bold text-purple-600">{analytics.success_rate}%</p>
                    </div>
                    <div className="bg-orange-50 p-4 rounded-lg">
                      <p className="text-sm text-gray-600">Active PSPs</p>
                      <p className="text-2xl font-bold text-orange-600">{analytics.active_psps}</p>
                    </div>
                  </div>
                ) : (
                  <div className="px-4 py-5 sm:px-6">
                    <p className="text-gray-500">Loading analytics...</p>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

export default AdminDashboard;



