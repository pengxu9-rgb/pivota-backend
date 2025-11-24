import axios from 'axios';
import { API_CONFIG } from './config';

const API_BASE_URL = API_CONFIG.BASE_URL;

// Create axios instance with default config
const apiClient = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
});

// Add auth token to requests if available
apiClient.interceptors.request.use((config) => {
  if (typeof window !== 'undefined') {
    const token =
      localStorage.getItem('merchant_token') || localStorage.getItem('token');
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
  }
  return config;
});

export const onboardingApi = {
  register: async (data: any) => {
    const response = await apiClient.post('/merchant/onboarding/register', data);
    return response.data;
  },

  submitKYC: async (merchantId: string, documents: any) => {
    const payload = {
      merchant_id: merchantId,
      documents: documents
    };
    const response = await apiClient.post('/merchant/onboarding/kyc/upload', payload);
    return response.data;
  },

  uploadDocuments: async (merchantId: string, files: File[]) => {
    // Convert files to metadata (we don't have real file storage yet)
    const documentsData: any = {};
    for (let i = 0; i < files.length; i++) {
      const file = files[i];
      documentsData[`document_${i + 1}`] = {
        filename: file.name,
        size: file.size,
        type: file.type,
        uploaded_at: new Date().toISOString()
      };
    }
    
    const payload = {
      merchant_id: merchantId,
      documents: documentsData
    };
    const response = await apiClient.post('/merchant/onboarding/kyc/upload', payload);
    return response.data;
  },

  setupPSP: async (merchantId: string, pspType: string, apiKey: string, additionalData?: any) => {
    const payload = {
      merchant_id: merchantId,
      psp_type: pspType,
      api_key: apiKey,
      ...additionalData
    };
    const response = await apiClient.post('/merchant/onboarding/setup-psp', payload);
    return response.data;
  },

  getDetails: async (merchantId: string) => {
    const response = await apiClient.get(`/merchant/onboarding/details/${merchantId}`);
    return response.data;
  },

  issueApiKey: async (merchantId: string) => {
    const response = await apiClient.post(`/merchant/onboarding/api-key/${merchantId}`);
    return response.data;
  }
};

export const platformOnboardingApi = {
  register: async (data: any) => {
    const response = await apiClient.post(API_CONFIG.ENDPOINTS.PLATFORM_ONBOARDING_REGISTER, data);
    return response.data;
  },

  getFeatureStatus: async () => {
    const response = await apiClient.get(API_CONFIG.ENDPOINTS.PLATFORM_ONBOARDING_FEATURE_STATUS);
    return response.data;
  },

  getDetails: async (onboardingId: string) => {
    const base = API_CONFIG.ENDPOINTS.PLATFORM_ONBOARDING_DETAILS;
    const response = await apiClient.get(`${base}/${encodeURIComponent(onboardingId)}`);
    return response.data;
  },

  listImportTasks: async (onboardingId: string) => {
    const base = API_CONFIG.ENDPOINTS.PLATFORM_ONBOARDING_IMPORT_TASKS;
    const response = await apiClient.get(`${base}/${encodeURIComponent(onboardingId)}/import-tasks`);
    return response.data;
  },

  validateReport: async (
    onboardingId: string,
    reportType: 'amazon' | 'temu',
    file: File
  ) => {
    const formData = new FormData();
    formData.append('report_type', reportType);
    formData.append('file', file);

    // Use fetch API instead of axios for FormData uploads
    // This avoids Content-Type conflicts with axios defaults
    const token = typeof window !== 'undefined' 
      ? localStorage.getItem('merchant_token') || localStorage.getItem('token')
      : null;

    const base = API_CONFIG.ENDPOINTS.PLATFORM_ONBOARDING_DETAILS;
    const url = `${API_BASE_URL}${base}/${encodeURIComponent(onboardingId)}/reports/validate`;
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: formData,
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: 'Validation failed' }));
      throw new Error(error.detail || 'Validation failed');
    }

    return await response.json();
  },

  validateOrders: async (
    onboardingId: string,
    platform: 'amazon' | 'temu',
    file: File
  ) => {
    const formData = new FormData();
    formData.append('platform', platform);
    formData.append('file', file);

    const token =
      typeof window !== 'undefined'
        ? localStorage.getItem('merchant_token') || localStorage.getItem('token')
        : null;

    const base = API_CONFIG.ENDPOINTS.PLATFORM_ONBOARDING_DETAILS;
    const url = `${API_BASE_URL}${base}/${encodeURIComponent(onboardingId)}/orders/validate`;
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: formData,
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: 'Validation failed' }));
      throw new Error(error.detail || 'Validation failed');
    }

    return await response.json();
  },

  uploadReport: async (
    onboardingId: string,
    reportType: 'amazon' | 'temu',
    file: File
  ) => {
    const formData = new FormData();
    formData.append('report_type', reportType);
    formData.append('file', file);

    // Use fetch API instead of axios for FormData uploads
    // This avoids Content-Type conflicts with axios defaults
    const token = typeof window !== 'undefined' 
      ? localStorage.getItem('merchant_token') || localStorage.getItem('token')
      : null;

    const base = API_CONFIG.ENDPOINTS.PLATFORM_ONBOARDING_DETAILS;
    const url = `${API_BASE_URL}${base}/${encodeURIComponent(onboardingId)}/reports/upload`;
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: formData,
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: 'Upload failed' }));
      throw new Error(error.detail || 'Upload failed');
    }

    return await response.json();
  },

  uploadOrders: async (
    onboardingId: string,
    platform: 'amazon' | 'temu',
    file: File
  ) => {
    const formData = new FormData();
    formData.append('platform', platform);
    formData.append('file', file);

    const token =
      typeof window !== 'undefined'
        ? localStorage.getItem('merchant_token') || localStorage.getItem('token')
        : null;

    const base = API_CONFIG.ENDPOINTS.PLATFORM_ONBOARDING_DETAILS;
    const url = `${API_BASE_URL}${base}/${encodeURIComponent(onboardingId)}/orders/upload`;
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: formData,
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: 'Upload failed' }));
      throw new Error(error.detail || 'Upload failed');
    }

    return await response.json();
  },

  listOrders: async (
    onboardingId: string,
    params?: { platform?: 'amazon' | 'temu' | 'shopify' | 'wix'; limit?: number; offset?: number }
  ) => {
    const base = API_CONFIG.ENDPOINTS.PLATFORM_ONBOARDING_DETAILS;
    const response = await apiClient.get(`${base}/${encodeURIComponent(onboardingId)}/orders`, {
      params: {
        platform: params?.platform,
        limit: params?.limit ?? 20,
        offset: params?.offset ?? 0,
      },
    });
    return response.data || response;
  },

  createACPCheckout: async (orderId: number) => {
    const response = await apiClient.post(`/platform-orders/${orderId}/create-acp-checkout`);
    return response.data;
  },

  getOrderStatus: async (orderId: number) => {
    const response = await apiClient.get(`/platform-orders/${orderId}/status`);
    return response.data;
  },

  markOrderShipped: async (orderId: number, trackingNumber: string, carrier?: string) => {
    const response = await apiClient.post(`/platform-orders/${orderId}/mark-shipped`, {
      tracking_number: trackingNumber,
      carrier,
    });
    return response.data;
  },

  getOrderableProducts: async (
    onboardingId: string,
    platform: 'amazon' | 'temu',
    limit: number = 20
  ) => {
    const response = await apiClient.get(
      `/products/v2/${encodeURIComponent(onboardingId)}`,
      {
        params: {
          platform,
          limit,
        },
      }
    );
    return response.data;
  },

  createPlatformOrderPoc: async (
    onboardingId: string,
    payload: {
      platform: 'amazon' | 'temu';
      platform_product_id: string;
      variant_id?: string;
      quantity: number;
      buyer_email?: string;
    }
  ) => {
    const base = API_CONFIG.ENDPOINTS.PLATFORM_ONBOARDING_DETAILS;
    const response = await apiClient.post(
      `${base}/${encodeURIComponent(onboardingId)}/orders/poc`,
      payload
    );
    return response.data;
  },
};

export const integrationsApi = {
  startShopifyOAuth: async (merchantId: string, shopDomain: string) => {
    const response = await apiClient.get('/integrations/shopify/oauth/start', {
      params: {
        merchant_id: merchantId,
        shop: shopDomain
      }
    });
    return response.data;
  },

  connectShopify: async (merchantId: string, shopDomain: string, accessToken: string, storeName?: string) => {
    const response = await apiClient.post('/integrations/shopify/connect', {
      merchant_id: merchantId,
      shop_domain: shopDomain,
      access_token: accessToken,
      store_name: storeName
    });
    return response.data;
  },

  connectWix: async (merchantId: string, data: any) => {
    const response = await apiClient.post(`/integrations/wix/connect/${merchantId}`, data);
    return response.data;
  }
};

export default apiClient;
