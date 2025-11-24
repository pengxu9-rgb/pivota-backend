import axios, { AxiosInstance, AxiosError } from 'axios';
import { API_CONFIG } from './config';

class ApiClient {
  private client: AxiosInstance;

  constructor() {
    this.client = axios.create({
      baseURL: API_CONFIG.BASE_URL,
      headers: {
        'Content-Type': 'application/json',
      },
    });

    // Request interceptor to add auth token
    this.client.interceptors.request.use(
      (config) => {
        const token = this.getAuthToken();
        if (token) {
          config.headers.Authorization = `Bearer ${token}`;
        }
        console.log(`🔄 API Request: ${config.method?.toUpperCase()} ${config.url}`);
        return config;
      },
      (error) => {
        console.error('❌ Request Error:', error);
        return Promise.reject(error);
      }
    );

    // Response interceptor for error handling
    this.client.interceptors.response.use(
      (response) => {
        console.log(`✅ API Response: ${response.status} ${response.config.url}`);
        return response;
      },
      (error: AxiosError) => {
        console.error(`❌ API Error: ${error.response?.status} ${error.config?.url}`);
        
        if (error.response?.status === 401) {
          // Clear auth and redirect to login
          this.clearAuth();
          if (typeof window !== 'undefined') {
            window.location.href = '/login';
          }
        }
        
        return Promise.reject(error);
      }
    );
  }

  private getAuthToken(): string | null {
    if (typeof window !== 'undefined') {
      return localStorage.getItem('merchant_token');
    }
    return null;
  }

  private clearAuth(): void {
    if (typeof window !== 'undefined') {
      localStorage.removeItem('merchant_token');
      localStorage.removeItem('merchant_user');
      localStorage.removeItem('merchant_id');
    }
  }

  // Auth methods
  async login(email: string, password: string) {
    const response = await this.client.post(API_CONFIG.ENDPOINTS.LOGIN, {
      email,
      password,
    });
    
    // Store auth data
    if ((response.data.success === true || response.data.status === 'success') && response.data.token) {
      localStorage.setItem('merchant_token', response.data.token);
      localStorage.setItem('merchant_user', JSON.stringify(response.data.user));
      localStorage.setItem('merchant_id', response.data.user.merchant_id || response.data.user.id);
    }
    
    return response.data;
  }

  async register(data: any) {
    const response = await this.client.post(API_CONFIG.ENDPOINTS.REGISTER, data);
    return response.data;
  }

  async logout() {
    this.clearAuth();
    window.location.href = '/login';
  }

  // Profile methods
  async getProfile() {
    const response = await this.client.get(API_CONFIG.ENDPOINTS.PROFILE);
    return response.data;
  }

  async updateProfile(data: any) {
    const response = await this.client.put(API_CONFIG.ENDPOINTS.UPDATE_PROFILE, data);
    return response.data;
  }

  // Products methods
  async getProducts() {
    const merchantId = localStorage.getItem('merchant_id') || '';
    if (!merchantId) return [];
    const response = await this.client.get(`/products/${merchantId}`);
    return response.data.products || [];
  }

  async getProductsV2(params?: { platform?: string; limit?: number; offset?: number }) {
    const merchantId = localStorage.getItem('merchant_id') || '';
    if (!merchantId) {
      return { products: [], total: 0, limit: params?.limit ?? 20, offset: params?.offset ?? 0 };
    }
    const response = await this.client.get(`/products/v2/${merchantId}`, {
      params: {
        platform: params?.platform,
        limit: params?.limit ?? 20,
        offset: params?.offset ?? 0,
      },
    });
    return response.data;
  }

  async getProduct(id: string) {
    const url = API_CONFIG.ENDPOINTS.PRODUCT_DETAIL.replace(':id', id);
    const response = await this.client.get(url);
    return response.data;
  }

  async createProduct(data: any) {
    const response = await this.client.post(API_CONFIG.ENDPOINTS.PRODUCT_CREATE, data);
    return response.data;
  }

  async updateProduct(id: string, data: any) {
    const url = API_CONFIG.ENDPOINTS.PRODUCT_UPDATE.replace(':id', id);
    const response = await this.client.put(url, data);
    return response.data;
  }

  async deleteProduct(id: string) {
    const url = API_CONFIG.ENDPOINTS.PRODUCT_DELETE.replace(':id', id);
    const response = await this.client.delete(url);
    return response.data;
  }

  async syncShopifyProducts(merchantId: string) {
    const response = await this.client.post(API_CONFIG.ENDPOINTS.SYNC_SHOPIFY, {
      merchant_id: merchantId,
    });
    return response.data;
  }

  // Orders methods
  async getOrders(params?: any) {
    const merchantId = localStorage.getItem('merchant_id') || '';
    if (!merchantId) return { orders: [], total: 0, limit: 50, offset: 0 };
    
    // Try the new merchant-specific endpoint first
    try {
      const response = await this.client.get(`/merchant/${merchantId}/orders`, { params });
      const data = response.data?.data || response.data;
      return {
        orders: data?.orders || [],
        total: data?.total || 0,
        limit: data?.limit || 50,
        offset: data?.offset || 0
      };
    } catch (error) {
      // Fallback to old endpoint
      const response = await this.client.get(`/orders/merchant/${merchantId}`, { params });
      const data = response.data?.data || response.data;
      return {
        orders: data?.orders || [],
        total: data?.total || 0,
        limit: data?.limit || 50,
        offset: data?.offset || 0
      };
    }
  }

  async getOrder(id: string) {
    const url = API_CONFIG.ENDPOINTS.ORDER_DETAIL.replace(':id', id);
    const response = await this.client.get(url);
    return response.data;
  }

  async updateOrder(id: string, data: any) {
    const url = API_CONFIG.ENDPOINTS.ORDER_UPDATE.replace(':id', id);
    const response = await this.client.patch(url, data);
    return response.data;
  }

  async refundOrder(id: string, amount?: number) {
    const url = API_CONFIG.ENDPOINTS.ORDER_REFUND.replace(':id', id);
    const response = await this.client.post(url, { amount });
    return response.data;
  }

  // Integration methods
  async connectShopify(merchantId: string, shopDomain: string, accessToken: string) {
    const response = await this.client.post('/integrations/shopify/connect', {
      merchant_id: merchantId,
      shop_domain: shopDomain,
      access_token: accessToken,
    });
    return response.data;
  }

  async disconnectShopify(merchantId: string) {
    const response = await this.client.post(API_CONFIG.ENDPOINTS.SHOPIFY_DISCONNECT, {
      merchant_id: merchantId,
    });
    return response.data;
  }

  async connectWix(merchantId: string, apiKey: string, siteId: string) {
    const response = await this.client.post(API_CONFIG.ENDPOINTS.WIX_CONNECT, {
      merchant_id: merchantId,
      api_key: apiKey,
      site_id: siteId,
    });
    return response.data;
  }

  async disconnectWix(merchantId: string) {
    const response = await this.client.post(API_CONFIG.ENDPOINTS.WIX_DISCONNECT, {
      merchant_id: merchantId,
    });
    return response.data;
  }

  async getConnectedStores(merchantId: string) {
    // Use dashboard routes that merge runtime and demo stores
    const response = await this.client.get(`/merchant/${merchantId}/integrations`);
    const raw = response.data?.data?.stores || [];
    // Normalize to UI expected fields
    return raw.map((s: any) => ({
      id: s.id || s.store_id || `${s.platform}-${s.domain || s.name}`,
      platform: s.platform,
      store_name: s.name || s.store_name || s.domain || 'Store',
      domain: s.domain || s.store_url || '',
      is_active: (s.status || '').toLowerCase() === 'connected' || s.is_active === true,
      product_count: s.product_count ?? 0,
      last_sync: s.last_sync || new Date().toISOString(),
    }));
  }

  // PSP methods
  async getPSPs(merchantId: string) {
    const response = await this.client.get(`/merchant/${merchantId}/psps`);
    const raw = response.data?.data?.psps || [];
    // Normalize to UI expected fields
    return raw.map((p: any) => ({
      id: p.id || p.provider,
      type: p.provider || p.type,
      name: p.name || (p.provider ? p.provider.charAt(0).toUpperCase() + p.provider.slice(1) : 'PSP'),
      is_active: (p.status || '').toLowerCase() === 'active' || p.is_active === true,
      success_rate: p.success_rate ?? 98.5,
      volume_today: p.volume_today ?? 0,
      transaction_count: p.transaction_count ?? 0,
    }));
  }

  // Sync products by platform
  async syncPlatformProducts(platform: string) {
    const p = (platform || '').toLowerCase();
    const merchantId = localStorage.getItem('merchant_id') || '';
    
    if (p === 'shopify') {
      return this.syncShopifyProducts(merchantId);
    }
    // Wix or others
    const response = await this.client.post(`/merchant/integrations/${p}/sync`);
    return response.data;
  }

  async connectPSP(merchantId: string, pspType: string, apiKey: string, options?: any) {
    const response = await this.client.post('/merchant/onboarding/setup-psp', {
      merchant_id: merchantId,
      psp_type: pspType,
      api_key: apiKey,
      test_mode: true,
      ...options,
    });
    return response.data;
  }

  async disconnectPSP(pspId: string) {
    const url = API_CONFIG.ENDPOINTS.PSP_DISCONNECT.replace(':id', pspId);
    const response = await this.client.post(url);
    return response.data;
  }

  async testPSP(pspId: string) {
    const url = API_CONFIG.ENDPOINTS.PSP_TEST.replace(':id', pspId);
    const response = await this.client.post(url);
    return response.data;
  }

  // Analytics methods
  async getAnalyticsDashboard(timeRange: string = '30d') {
    // Use the new dashboard stats endpoint
    const response = await this.client.get('/merchant/dashboard/stats');
    return response.data?.data || response.data;
  }

  async getAnalyticsTrends(params?: {
    metric?: 'gmv' | 'orders' | 'aov' | 'success_rate' | 'refunds';
    range?: '1d' | '7d' | '30d' | '90d';
    interval?: 'day' | 'week';
    psp?: string;
    currency?: string;
    compare?: boolean;
    mode?: 'gross' | 'net';
  }) {
    const response = await this.client.get('/merchant/analytics/trends', { params });
    return response.data?.data || response.data;
  }

  async exportAnalyticsTrendsCSV(params?: {
    metric?: 'gmv' | 'orders' | 'aov' | 'success_rate' | 'refunds';
    range?: '1d' | '7d' | '30d' | '90d';
    interval?: 'day' | 'week';
    psp?: string;
    currency?: string;
    compare?: boolean;
    mode?: 'gross' | 'net';
  }) {
    const response = await this.client.get('/merchant/analytics/trends.csv', {
      params,
      responseType: 'blob',
    });
    return response.data as Blob;
  }

  async getOrderAnalytics() {
    const response = await this.client.get(API_CONFIG.ENDPOINTS.ANALYTICS_ORDERS);
    return response.data;
  }

  async getRevenueAnalytics() {
    const response = await this.client.get(API_CONFIG.ENDPOINTS.ANALYTICS_REVENUE);
    return response.data;
  }

  // Extended methods for dashboard features
  // [Wait] Duplicate method detected. Keeping original login/logout/etc.
  // Merging new methods from "Extended methods" section into existing class structure...

  async getMcpSummary() {
    const response = await this.client.get('/merchant/mcp/summary');
    return response.data?.data || response.data;
  }

  async addProduct(productData: any) {
    const response = await this.client.post('/merchant/products/add', productData);
    return response.data;
  }

  async getOrderDetails(orderId: string) {
    const response = await this.client.get(`/merchant/orders/${orderId}`);
    return response.data?.data || response.data;
  }

  async markOrderShipped(orderId: string, trackingData: { tracking_number: string; carrier: string }) {
    const response = await this.client.post(`/merchant/orders/${orderId}/ship`, trackingData);
    return response.data;
  }

  async exportOrders() {
    const response = await this.client.post('/merchant/orders/export');
    return response.data;
  }

  async connectStore(storeData: any) {
    const response = await this.client.post('/merchant/integrations/store/connect', storeData);
    return response.data;
  }

  async connectPSPProvider(pspData: any) {
    const response = await this.client.post('/merchant/integrations/psp/connect', pspData);
    return response.data;
  }

  // New integration methods
  async getIntegrations(type?: string) {
    const params = type ? { integration_type: type } : {};
    const response = await this.client.get('/merchant/integrations', { params });
    return response.data.integrations || [];
  }

  async getIntegrationSchemas(type?: string, provider?: string) {
    const params: any = {};
    if (type) params.integration_type = type;
    if (provider) params.provider = provider;
    const response = await this.client.get('/merchant/integrations/schemas', { params });
    return response.data.schemas || {};
  }

  async createIntegration(data: {
    integration_type: string;
    provider: string;
    display_name: string;
    credentials: any;
    settings?: any;
  }) {
    const response = await this.client.post('/merchant/integrations', data);
    return response.data;
  }

  async testIntegration(integrationId: number, credentials?: any) {
    const response = await this.client.post(`/merchant/integrations/${integrationId}/test`, {
      credentials
    });
    return response.data;
  }

  async updateIntegration(integrationId: number, data: any) {
    const response = await this.client.put(`/merchant/integrations/${integrationId}`, data);
    return response.data;
  }

  async deleteIntegration(integrationId: number) {
    const response = await this.client.delete(`/merchant/integrations/${integrationId}`);
    return response.data;
  }

  async syncIntegrationWebhooks(integrationId: number, webhookUrl: string) {
    const response = await this.client.post(`/merchant/integrations/${integrationId}/webhooks/sync`, 
      webhookUrl, // Send as raw string in body
      { headers: { 'Content-Type': 'text/plain' } }
    );
    return response.data;
  }

  async getIntegrationLogs(integrationId: number, logType?: string, limit: number = 100) {
    const params: any = { limit };
    if (logType) params.log_type = logType;
    const response = await this.client.get(`/merchant/integrations/${integrationId}/logs`, { params });
    return response.data;
  }

  async changePassword(passwordData: any) {
    const response = await this.client.post('/merchant/security/change-password', passwordData);
    return response.data;
  }

  async enable2FA() {
    const response = await this.client.post('/merchant/security/enable-2fa');
    return response.data;
  }

  // Webhook methods
  async getWebhookConfig() {
    const response = await this.client.get(API_CONFIG.ENDPOINTS.WEBHOOK_CONFIG);
    return response.data;
  }

  async updateWebhookConfig(config: any) {
    const response = await this.client.put(API_CONFIG.ENDPOINTS.WEBHOOK_CONFIG, config);
    return response.data;
  }

  async getWebhookSecret() {
    const response = await this.client.get(API_CONFIG.ENDPOINTS.WEBHOOK_SECRET);
    return response.data;
  }

  async rotateWebhookSecret() {
    const response = await this.client.post(`${API_CONFIG.ENDPOINTS.WEBHOOK_SECRET}/rotate`);
    return response.data;
  }

  async testWebhook(url: string, event: string) {
    const response = await this.client.post(API_CONFIG.ENDPOINTS.WEBHOOK_TEST, { url, event });
    return response.data;
  }

  async getWebhookLogs(limit: number = 20) {
    const response = await this.client.get(API_CONFIG.ENDPOINTS.WEBHOOK_LOGS, {
      params: { limit },
    });
    return response.data.deliveries || [];
  }

  // [Phase 6] Commission methods
  async getCommissionOffers(merchantId: string) {
    const response = await this.client.get(`/merchants/${merchantId}/commission/offers`);
    return response.data;
  }

  async createCommissionOffer(merchantId: string, data: {
    agent_type?: string | null;
    offered_commission_rate: number;
    min_order_amount: number;
    max_order_amount?: number;
    notes?: string;
  }) {
    const response = await this.client.post(`/merchants/${merchantId}/commission/offers`, data);
    return response.data;
  }

  async deleteCommissionOffer(merchantId: string, offerId: number) {
    const response = await this.client.delete(`/merchants/${merchantId}/commission/offers/${offerId}`);
    return response.data;
  }

  async markOrderShipped(orderId: string, trackingNumber: string, carrier?: string) {
    const merchantId = localStorage.getItem('merchant_id') || '';
    if (!merchantId) throw new Error('Merchant ID not found');
    const response = await this.client.post(`/merchant/orders/${orderId}/ship`, {
      tracking_number: trackingNumber,
      carrier: carrier,
    });
    return response.data;
  }

  async refundOrderV2(orderId: string, amount: number, reason: string) {
    const response = await this.client.post(`/merchant/orders/${orderId}/refund`, {
      amount,
      reason,
      source: 'pivota_merchant'
    });
    return response.data;
  }

  async getOrderRefunds(orderId: string) {
    const response = await this.client.get(`/merchant/orders/${orderId}/refunds`);
    return response.data;
  }

  // Generic HTTP methods
  async get(url: string, params?: any) {
    const response = await this.client.get(url, { params });
    return response;
  }

  async post(url: string, data?: any) {
    const response = await this.client.post(url, data);
    return response;
  }

  async put(url: string, data?: any) {
    const response = await this.client.put(url, data);
    return response;
  }

  async patch(url: string, data?: any) {
    const response = await this.client.patch(url, data);
    return response;
  }

  async delete(url: string) {
    const response = await this.client.delete(url);
    return response;
  }
}

// Export singleton instance
export const apiClient = new ApiClient();

// Export types
export interface MerchantUser {
  id: string;
  merchant_id: string;
  email: string;
  business_name: string;
  status: string;
}

export interface Product {
  id: string;
  name: string;
  price: number;
  stock: number;
  description?: string;
  image_url?: string;
  sku?: string;
}

export interface Order {
  id: string;
  order_number: string;
  customer_email: string;
  total_amount: number;
  status: string;
  created_at: string;
  items: OrderItem[];
}

export interface OrderItem {
  product_id: string;
  product_name: string;
  quantity: number;
  price: number;
}

export interface Store {
  id: string;
  platform: 'shopify' | 'wix' | 'woocommerce';
  store_name: string;
  domain: string;
  is_active: boolean;
  product_count: number;
  last_sync: string;
}

export interface PSP {
  id: string;
  type: string;
  name: string;
  is_active: boolean;
  success_rate: number;
  volume_today: number;
  transaction_count: number;
}
