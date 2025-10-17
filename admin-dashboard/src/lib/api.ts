import axios from 'axios';

const API_BASE_URL = 'https://web-production-fedb.up.railway.app';

// Get stored JWT token
export const getAdminToken = () => {
  return localStorage.getItem('pivota_admin_jwt') || '';
};

// Create axios instance with auth
const api = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
});

// Add auth token to requests
api.interceptors.request.use((config) => {
  const token = getAdminToken();
  console.log('🔑 API Request - Token exists:', !!token);
  console.log('🔑 Token length:', token.length);
  console.log('🔑 Token preview:', token.substring(0, 20) + '...');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  console.log('📡 Making API request:', {
    method: config.method,
    url: config.url,
    baseURL: config.baseURL,
    fullURL: `${config.baseURL}${config.url}`,
    headers: config.headers,
  });
  return config;
});

// Add response interceptor for debugging
api.interceptors.response.use(
  (response) => {
    console.log('✅ API response received:', {
      status: response.status,
      url: response.config.url,
      data: response.data,
    });
    return response;
  },
  (error) => {
    console.error('❌ API error:', {
      status: error.response?.status,
      url: error.config?.url,
      data: error.response?.data,
      message: error.message,
    });
    return Promise.reject(error);
  }
);

export interface Merchant {
  merchant_id: string;
  business_name: string;
  store_url: string;
  website?: string;
  region: string;
  contact_email: string;
  contact_phone?: string;
  status: string;
  auto_approved?: boolean;
  approval_confidence?: number;
  full_kyb_deadline?: string;
  psp_connected: boolean;
  psp_type?: string;
  api_key?: string;
  kyc_documents?: any[];
  rejection_reason?: string;
  created_at: string;
  updated_at?: string;
  // MCP 相关（占位，后端后续提供）
  mcp_connected?: boolean;
  mcp_platform?: string;
}

export const merchantApi = {
  // Get all merchants
  async getAll(): Promise<Merchant[]> {
    const response = await api.get('/merchant/onboarding/all');
    return response.data.merchants || [];
  },

  // Get merchant details
  async getDetails(merchantId: string): Promise<Merchant> {
    const response = await api.get(`/merchant/onboarding/details/${merchantId}`);
    return response.data.merchant; // 后端返回 {status, merchant}
  },

  // Delete merchant
  async delete(merchantId: string): Promise<void> {
    await api.delete(`/merchant/onboarding/delete/${merchantId}`);
  },

  // Update KYB status
  async updateKYB(merchantId: string, status: string, reason?: string): Promise<void> {
    await api.post(`/merchant/onboarding/kyb/${merchantId}`, {
      status,
      reason,
    });
  },

  // Upload KYB documents
  async uploadDocuments(merchantId: string, files: File[]): Promise<void> {
    const formData = new FormData();
    files.forEach((file) => {
      formData.append('files', file);
    });
    await api.post(`/merchant/onboarding/upload/${merchantId}`, formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
    });
  },
};

export const integrationsApi = {
  async connectShopify(merchantId: string, shopDomain?: string, accessToken?: string) {
    const response = await api.post('/integrations/shopify/connect', {
      merchant_id: merchantId,
      shop_domain: shopDomain,
      access_token: accessToken,
    });
    return response.data;
  },

  async syncShopifyProducts(merchantId: string, limit = 20, shopDomain?: string, accessToken?: string) {
    const response = await api.post('/integrations/shopify/products/sync', {
      merchant_id: merchantId,
      limit,
      shop_domain: shopDomain,
      access_token: accessToken,
    });
    return response.data;
  },
};

export default api;

