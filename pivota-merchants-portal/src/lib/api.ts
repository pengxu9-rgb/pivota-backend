import axios from 'axios';

const API_BASE_URL = 'https://web-production-fedb.up.railway.app';

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
});

// Add auth token to requests
apiClient.interceptors.request.use((config) => {
  const token = localStorage.getItem('merchant_token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// Onboarding API
export const onboardingApi = {
  async register(data: any) {
    const response = await apiClient.post('/merchant/onboarding/register', data);
    return response.data;
  },

  async uploadDocuments(merchantId: string, documents: any) {
    const response = await apiClient.post('/merchant/onboarding/kyc/upload', {
      merchant_id: merchantId,
      documents,
    });
    return response.data;
  },

  async setupPSP(merchantId: string, pspType: string, apiKey: string) {
    const response = await apiClient.post('/merchant/onboarding/setup-psp', {
      merchant_id: merchantId,
      psp_type: pspType,
      api_key: apiKey,
      test_mode: true,
    });
    return response.data;
  },

  async getStatus(merchantId: string) {
    const response = await apiClient.get(`/merchant/onboarding/status/${merchantId}`);
    return response.data;
  },
};

// PSP Management API
export const pspApi = {
  async getPSPs(merchantId: string) {
    const response = await apiClient.get(`/merchant/${merchantId}/psps`);
    return response.data?.data?.psps || [];
  },

  async testPSP(pspId: string) {
    const response = await apiClient.post(`/merchant/psps/${pspId}/test`);
    return response.data;
  },
};

// Routing API
export const routingApi = {
  async getRoutingConfig(merchantId: string) {
    const response = await apiClient.get(`/merchant/${merchantId}/routing`);
    return response.data;
  },

  async updateRoutingConfig(merchantId: string, config: any) {
    const response = await apiClient.put(`/merchant/${merchantId}/routing`, config);
    return response.data;
  },
};

// Orders API
export const ordersApi = {
  async getOrders(merchantId: string, params?: any) {
    const response = await apiClient.get(`/merchant/${merchantId}/orders`, { params });
    return response.data?.orders || [];
  },

  async getOrder(orderId: string) {
    const response = await apiClient.get(`/merchant/orders/${orderId}`);
    return response.data;
  },
};

// Products API
export const productsApi = {
  async getProducts(merchantId: string) {
    const response = await apiClient.get(`/products/${merchantId}`);
    return response.data?.products || [];
  },

  async syncProducts(merchantId: string, platform: string) {
    const response = await apiClient.post(`/merchant/integrations/${platform}/sync`, {
      merchant_id: merchantId,
    });
    return response.data;
  },
};

// Settings API
export const settingsApi = {
  async getProfile() {
    const response = await apiClient.get('/merchant/profile');
    return response.data;
  },

  async updateProfile(data: any) {
    const response = await apiClient.put('/merchant/profile', data);
    return response.data;
  },
};

// Analytics API
export const analyticsApi = {
  async getDashboard(merchantId: string, timeRange: string = '30d') {
    const response = await apiClient.get(`/merchant/${merchantId}/analytics`, {
      params: { range: timeRange },
    });
    return response.data;
  },
};

// Auth API
export const authApi = {
  async login(email: string, password: string) {
    const response = await apiClient.post('/merchant/login', { email, password });
    return response.data;
  },

  async logout() {
    localStorage.removeItem('merchant_token');
    localStorage.removeItem('merchant_id');
  },
};

// Integrations API
export const integrationsApi = {
  async connectShopify(merchantId: string, shopDomain: string, accessToken: string) {
    const response = await apiClient.post('/integrations/shopify/connect', {
      merchant_id: merchantId,
      shop_domain: shopDomain,
      access_token: accessToken,
    });
    return response.data;
  },

  async connectWix(merchantId: string, apiKey: string, siteId: string) {
    const response = await apiClient.post('/integrations/wix/connect', {
      merchant_id: merchantId,
      api_key: apiKey,
      site_id: siteId,
    });
    return response.data;
  },

  async getStores(merchantId: string) {
    const response = await apiClient.get(`/merchant/${merchantId}/integrations`);
    return response.data?.data?.stores || [];
  },
};

// Webhook API
export const webhookApi = {
  async getConfig(merchantId: string) {
    const response = await apiClient.get(`/merchant/${merchantId}/webhooks`);
    return response.data;
  },

  async updateConfig(merchantId: string, config: any) {
    const response = await apiClient.put(`/merchant/${merchantId}/webhooks`, config);
    return response.data;
  },
};

// PSP Management API
export const pspManagementApi = {
  async connectPSP(merchantId: string, pspType: string, apiKey: string) {
    const response = await apiClient.post('/merchant/onboarding/setup-psp', {
      merchant_id: merchantId,
      psp_type: pspType,
      api_key: apiKey,
      test_mode: true,
    });
    return response.data;
  },
};
