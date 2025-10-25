/**
 * Pivota Agent Client
 */
import axios, { AxiosInstance, AxiosError } from 'axios';
import {
  PivotaAgentConfig,
  Merchant,
  Product,
  ProductSearchParams,
  ProductSearchResult,
  Order,
  CreateOrderParams,
  Payment,
  CreatePaymentParams,
} from './types';
import {
  PivotaAPIError,
  AuthenticationError,
  RateLimitError,
  NotFoundError,
  ValidationError,
} from './exceptions';

export class PivotaAgentClient {
  private client: AxiosInstance;
  private apiKey?: string;

  constructor(config: PivotaAgentConfig) {
    this.apiKey = config.apiKey;
    
    this.client = axios.create({
      baseURL: config.baseUrl || 'https://web-production-fedb.up.railway.app/agent/v1',
      timeout: config.timeout || 30000,
      headers: {
        'User-Agent': 'Pivota-TypeScript-SDK/1.0.0',
        ...(this.apiKey && { 'X-API-Key': this.apiKey }),
      },
    });

    // Response interceptor for error handling
    this.client.interceptors.response.use(
      (response) => response,
      (error: AxiosError) => {
        if (error.response) {
          const status = error.response.status;
          const data: any = error.response.data;
          const message = data?.detail || 'API request failed';

          if (status === 401) {
            throw new AuthenticationError(message);
          } else if (status === 429) {
            const retryAfter = error.response.headers['retry-after'];
            throw new RateLimitError(message, retryAfter ? parseInt(retryAfter) : 60);
          } else if (status === 404) {
            throw new NotFoundError(message);
          } else if (status === 400) {
            throw new ValidationError(message);
          } else {
            throw new PivotaAPIError(message, status);
          }
        }
        throw new PivotaAPIError(error.message);
      }
    );
  }

  // ========================================================================
  // Authentication
  // ========================================================================

  /**
   * Create a new agent and get API key
   */
  static async createAgent(
    agentName: string,
    agentEmail: string,
    description?: string,
    baseUrl?: string
  ): Promise<PivotaAgentClient> {
    const tempClient = new PivotaAgentClient({ baseUrl });
    
    const response = await tempClient.client.post('/auth', {
      agent_name: agentName,
      agent_email: agentEmail,
      description,
    });

    const apiKey = response.data.api_key;
    if (!apiKey) {
      throw new PivotaAPIError('Failed to get API key from response');
    }

    return new PivotaAgentClient({ apiKey, baseUrl });
  }

  /**
   * Check API health
   */
  async healthCheck(): Promise<{ status: string; version: string }> {
    const response = await this.client.get('/health');
    return response.data;
  }

  // ========================================================================
  // Merchants
  // ========================================================================

  /**
   * List available merchants
   */
  async listMerchants(params?: {
    status?: string;
    limit?: number;
    offset?: number;
  }): Promise<Merchant[]> {
    const response = await this.client.get('/merchants', { params });
    return response.data.merchants || [];
  }

  // ========================================================================
  // Products
  // ========================================================================

  /**
   * Search products across merchants
   */
  async searchProducts(params: ProductSearchParams): Promise<ProductSearchResult> {
    const response = await this.client.get('/products/search', { params });
    return response.data;
  }

  // ========================================================================
  // Orders
  // ========================================================================

  /**
   * Create a new order
   */
  async createOrder(params: CreateOrderParams): Promise<Order> {
    const response = await this.client.post('/orders/create', params);
    return response.data;
  }

  /**
   * Get order by ID
   */
  async getOrder(orderId: string): Promise<Order> {
    const response = await this.client.get(`/orders/${orderId}`);
    return response.data;
  }

  /**
   * List orders
   */
  async listOrders(params?: {
    merchant_id?: string;
    status?: string;
    limit?: number;
    offset?: number;
  }): Promise<{ orders: Order[]; pagination: any }> {
    const response = await this.client.get('/orders', { params });
    return response.data;
  }

  // ========================================================================
  // Payments
  // ========================================================================

  /**
   * Create payment for an order
   */
  async createPayment(params: CreatePaymentParams): Promise<Payment> {
    const response = await this.client.post('/payments', params);
    return response.data;
  }

  /**
   * Get payment status
   */
  async getPayment(paymentId: string): Promise<Payment> {
    const response = await this.client.get(`/payments/${paymentId}`);
    return response.data.payment;
  }

  // ========================================================================
  // Analytics
  // ========================================================================

  /**
   * Get analytics summary
   */
  async getAnalyticsSummary(): Promise<any> {
    const response = await this.client.get('/analytics/summary');
    return response.data;
  }
}





