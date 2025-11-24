// API Configuration
export const API_CONFIG = {
  BASE_URL: process.env.NEXT_PUBLIC_API_URL || 'https://web-production-fedb.up.railway.app',
  ENDPOINTS: {
    // Auth
    LOGIN: '/api/auth/login',
    REGISTER: '/merchant/onboarding/register',
    LOGOUT: '/auth/signout',
    
    // Merchant Profile
    PROFILE: '/merchant/profile',
    UPDATE_PROFILE: '/merchant/profile',
    
    // Products
    PRODUCTS_LIST: '/products',
    PRODUCT_DETAIL: '/products/:id',
    PRODUCT_CREATE: '/products',
    PRODUCT_UPDATE: '/products/:id',
    PRODUCT_DELETE: '/products/:id',
    SYNC_SHOPIFY: '/integrations/shopify/products/sync',
    
    // Orders
    ORDERS_LIST: '/orders',
    ORDER_DETAIL: '/orders/:id',
    ORDER_UPDATE: '/orders/:id',
    ORDER_REFUND: '/orders/:id/refund',
    
    // Integrations
    SHOPIFY_CONNECT: '/integrations/shopify/connect',
    SHOPIFY_DISCONNECT: '/integrations/shopify/disconnect',
    WIX_CONNECT: '/integrations/wix/connect',
    WIX_DISCONNECT: '/integrations/wix/disconnect',
    STORES_LIST: '/merchant/:merchantId/integrations',
    
    // PSP
    PSP_LIST: '/merchant/:merchantId/psps',
    PSP_CONNECT: '/merchant/onboarding/setup-psp',
    PSP_DISCONNECT: '/merchant/psp/:id/disconnect',
    PSP_TEST: '/merchant/psp/:id/test',
    
    // Analytics
    ANALYTICS_DASHBOARD: '/analytics/dashboard',
    ANALYTICS_ORDERS: '/analytics/orders',
    ANALYTICS_REVENUE: '/analytics/revenue',
    
    // Webhooks
    WEBHOOK_CONFIG: '/merchant/webhooks/config',
    WEBHOOK_SECRET: '/merchant/webhooks/secret',
    WEBHOOK_TEST: '/merchant/webhooks/test',
    WEBHOOK_LOGS: '/merchant/webhooks/deliveries',

    // Platform Merchant Onboarding v2 (behind feature flag)
    PLATFORM_ONBOARDING_REGISTER: '/platform-onboarding/register',
    PLATFORM_ONBOARDING_FEATURE_STATUS: '/platform-onboarding/feature-status',
    PLATFORM_ONBOARDING_DETAILS: '/platform-onboarding',
    PLATFORM_ONBOARDING_IMPORT_TASKS: '/platform-onboarding',
  }
};

export const APP_CONFIG = {
  NAME: 'Pivota Merchant Portal',
  VERSION: '2.0.0',
  SUPPORT_EMAIL: 'support@pivota.cc',
};

export const FEATURE_FLAGS = {
  PLATFORM_ONBOARDING_V2:
    (process.env.NEXT_PUBLIC_FEATURE_PLATFORM_ONBOARDING_V2 || 'true').toLowerCase() === 'true',
  PLATFORM_ORDERS_V1: 
    (process.env.NEXT_PUBLIC_FEATURE_PLATFORM_ORDERS_V1 || 'false').toLowerCase() === 'true',
  PLATFORM_ORDERS_ACP: 
    (process.env.NEXT_PUBLIC_FEATURE_PLATFORM_ORDERS_ACP || 'false').toLowerCase() === 'true',
};
