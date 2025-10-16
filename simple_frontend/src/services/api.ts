import axios from 'axios';
import { User, PSP, RoutingRule, Merchant, SystemLog, ApiKey, Analytics } from '../types';

const API_BASE_URL = 'https://pivota-dashboard.onrender.com';

// Create axios instance
const api = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
  timeout: 60000, // 60 second timeout for Render cold starts
});

// Add request interceptor for debugging
api.interceptors.request.use(
  (config) => {
    console.log('🚀 Making API request:', {
      method: config.method?.toUpperCase(),
      url: config.url,
      baseURL: config.baseURL,
      fullURL: `${config.baseURL}${config.url}`,
      headers: config.headers,
      data: config.data
    });
    return config;
  },
  (error) => {
    console.error('❌ Request interceptor error:', error);
    return Promise.reject(error);
  }
);

// Add auth token to requests
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('auth_token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// Handle auth errors
api.interceptors.response.use(
  (response) => {
    console.log('✅ API response received:', {
      status: response.status,
      url: response.config.url,
      data: response.data
    });
    return response;
  },
  (error) => {
    console.error('❌ API error - Full error object:', error);
    console.error('❌ API error details:', {
      status: error.response?.status,
      statusText: error.response?.statusText,
      url: error.config?.url,
      message: error.message,
      responseData: error.response?.data,
      code: error.code,
      stack: error.stack
    });
    
    if (error.response?.status === 401) {
      console.log('🔐 401 Unauthorized - redirecting to login');
      localStorage.removeItem('auth_token');
      window.location.href = '/login';
    }
    return Promise.reject(error);
  }
);

// Export the base axios instance
export { api };

// Auth API
export const authApi = {
  signin: async (email: string, password: string) => {
    const response = await api.post('/auth/signin', { email, password });
    return response.data;
  },

  signup: async (email: string, password: string, role: string = 'employee') => {
    const response = await api.post('/auth/signup', { email, password, role });
    return response.data;
  },

  me: async () => {
    const response = await api.get('/auth/me');
    return response.data;
  },

  getUsers: async () => {
    const response = await api.get('/auth/admin/users');
    return response.data;
  },

  approveUser: async (userId: string, approved: boolean) => {
    const response = await api.post(`/auth/admin/users/${userId}/approve`, { approved });
    return response.data;
  },

  changeUserRole: async (userId: string, role: string) => {
    const response = await api.post(`/auth/admin/users/${userId}/role`, { role });
    return response.data;
  },
};

// Merchant API
export const merchantApi = {
  onboard: async (merchantData: any) => {
    const response = await api.post('/merchants/onboard', merchantData);
    return response.data;
  },
  uploadDocument: async (merchantId: number | string, documentType: string, file: File) => {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('document_type', documentType);
    // Phase 2 merchant uses merch_ string id -> onboarding endpoint
    if (typeof merchantId === 'string' && merchantId.startsWith('merch_')) {
      const response = await api.post(`/merchant/onboarding/kyc/upload/file/${merchantId}` , formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
        timeout: 60000,
      });
      return response.data;
    }
    const response = await api.post(`/merchants/${merchantId}/documents/upload`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 60000,
    });
    return response.data;
  },

  getMerchant: async (merchantId: number) => {
    const response = await api.get(`/merchants/${merchantId}`);
    return response.data;
  },

  listMerchants: async (status?: string) => {
    const response = await api.get('/merchants/', { params: { status } });
    return response.data;
  },

  approve: async (merchantId: number) => {
    const response = await api.post(`/merchants/${merchantId}/approve`);
    return response.data;
  },

  reject: async (merchantId: number, reason: string) => {
    console.log('🔴 Rejecting merchant API call:', { merchantId, reason });
    try {
      const response = await api.post(`/merchants/${merchantId}/reject`, { 
        status: 'rejected',
        rejection_reason: reason 
      });
      console.log('✅ Reject response:', response.data);
      return response.data;
    } catch (error: any) {
      console.error('❌ Reject API error:', {
        message: error.message,
        code: error.code,
        response: error.response?.data,
        status: error.response?.status
      });
      throw error;
    }
  },

  deleteMerchant: async (merchantId: number) => {
    const response = await api.delete(`/merchants/${merchantId}`);
    return response.data;
  },
  deleteOnboardingByMerchantId: async (merchantId: string) => {
    const response = await api.delete(`/merchant/onboarding/delete/${merchantId}`);
    return response.data;
  },

  verifyDocument: async (documentId: number) => {
    const response = await api.post(`/merchants/documents/${documentId}/verify`);
    return response.data;
  },
};

// Admin API
export const adminApi = {
  // Dashboard
  getDashboard: async () => {
    const response = await api.get('/admin/dashboard');
    return response.data;
  },

  // PSP Management
  getPSPStatus: async (): Promise<{ status: string; psp: Record<string, PSP> }> => {
    const response = await api.get('/admin/psp/status');
    return response.data;
  },

  getPSPList: async () => {
    const response = await api.get('/admin/psp/list');
    return response.data;
  },

  testPSP: async (pspId: string) => {
    const response = await api.post(`/admin/psp/${pspId}/test`);
    return response.data;
  },

  togglePSP: async (pspId: string, enable: boolean) => {
    const response = await api.post(`/admin/psp/${pspId}/toggle`, { enable });
    return response.data;
  },

  addPSP: async (pspConfig: {
    psp_type: string;
    api_key: string;
    webhook_secret: string;
    merchant_account?: string;
    enabled?: boolean;
    sandbox_mode?: boolean;
  }) => {
    const response = await api.post('/admin/psp/add', pspConfig);
    return response.data;
  },

  // Routing Rules
  getRoutingRules: async (): Promise<{ rules: RoutingRule[] }> => {
    const response = await api.get('/admin/routing/rules');
    return response.data;
  },

  addRoutingRule: async (rule: {
    name: string;
    rule_type: string;
    conditions: Record<string, any>;
    target_psp: string;
    priority?: number;
    enabled?: boolean;
  }) => {
    const response = await api.post('/admin/routing/rules/add', rule);
    return response.data;
  },

  toggleRoutingRule: async (ruleId: string, enabled: boolean) => {
    const response = await api.post(`/admin/routing/rules/${ruleId}/toggle`, { enabled });
    return response.data;
  },

  // Merchant KYB
  getMerchantKYB: async (): Promise<{ merchants: Record<string, Merchant> }> => {
    const response = await api.get('/admin/merchants/kyb/status');
    return response.data;
  },

  updateMerchantKYB: async (merchantId: string, status: string, notes?: string) => {
    const response = await api.post('/admin/merchants/kyb/update', {
      merchant_id: merchantId,
      kyb_status: status,
      notes,
    });
    return response.data;
  },

  onboardMerchant: async (merchantData: {
    name: string;
    store_url: string;
    platform: string;
  }) => {
    const response = await api.post('/admin/merchants/onboard', merchantData);
    return response.data;
  },

  // System Logs
  getSystemLogs: async (limit: number = 50, hours: number = 24): Promise<{ logs: SystemLog[] }> => {
    const response = await api.get(`/admin/logs?limit=${limit}&hours=${hours}`);
    return response.data;
  },

  // Developer Tools
  getApiKeys: async (): Promise<{ api_keys: ApiKey[] }> => {
    const response = await api.get('/admin/dev/api-keys');
    return response.data;
  },

  generateApiKey: async (name: string, permissions: string[]) => {
    const response = await api.post('/admin/dev/api-keys/generate', { name, permissions });
    return response.data;
  },

  toggleSandboxMode: async (enabled: boolean) => {
    const response = await api.post('/admin/dev/sandbox/toggle', { enabled });
    return response.data;
  },

  // Analytics
  getAnalytics: async (days: number = 30): Promise<Analytics> => {
    const response = await api.get(`/admin/analytics/overview?days=${days}`);
    return response.data;
  },

  // Test endpoints
  testConnection: async () => {
    const response = await api.get('/admin/test');
    return response.data;
  },
};

// Better error message extractor
export function getApiErrorMessage(err: any): string {
  return err?.response?.data?.detail || err?.message || err?.code || 'Unknown error';
}
