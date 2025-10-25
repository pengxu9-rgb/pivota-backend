/**
 * Pivota Agent SDK Types
 */

export interface PivotaAgentConfig {
  apiKey?: string;
  baseUrl?: string;
  timeout?: number;
}

export interface Merchant {
  merchant_id: string;
  business_name: string;
  status: string;
  store_url?: string;
  region?: string;
  contact_email: string;
  psp_connected?: boolean;
  psp_type?: string;
  mcp_connected?: boolean;
  mcp_platform?: string;
  product_count?: number;
  last_synced?: string;
  products_expired?: boolean;
  created_at: string;
}

export interface Product {
  id: string;
  name: string;
  description: string;
  price: number;
  currency: string;
  category?: string;
  in_stock: boolean;
  image_url?: string;
  url?: string;
  merchant_id: string;
  merchant_name: string;
  platform: string;
  relevance_score?: number;
  cached_at?: string;
}

export interface ProductSearchParams {
  query?: string;
  merchant_id?: string;
  category?: string;
  min_price?: number;
  max_price?: number;
  in_stock?: boolean;
  limit?: number;
  offset?: number;
}

export interface ProductSearchResult {
  status: string;
  products: Product[];
  pagination: {
    total: number;
    limit: number;
    offset: number;
    has_more: boolean;
  };
}

export interface OrderItem {
  product_id: string;
  quantity: number;
}

export interface ShippingAddress {
  street: string;
  city: string;
  state: string;
  zip: string;
  country: string;
}

export interface CreateOrderParams {
  merchant_id: string;
  items: OrderItem[];
  customer_email: string;
  shipping_address?: ShippingAddress;
  currency?: string;
}

export interface Order {
  order_id: string;
  merchant_id: string;
  status: string;
  total_amount: number;
  currency: string;
  items: OrderItem[];
  customer_email: string;
  shipping_address?: ShippingAddress;
  created_at: string;
  payment_status?: string;
  tracking_number?: string;
}

export interface PaymentMethod {
  type: string;
  token?: string;
  card_last4?: string;
  brand?: string;
}

export interface CreatePaymentParams {
  order_id: string;
  payment_method: PaymentMethod;
  return_url?: string;
  idempotency_key?: string;
}

export interface Payment {
  status: string;
  payment_id: string;
  payment_intent_id: string;
  client_secret?: string;
  amount: number;
  currency: string;
  psp_used: string;
  next_action?: {
    type: string;
    redirect_url?: string;
  };
  error?: string;
  created_at: string;
}





