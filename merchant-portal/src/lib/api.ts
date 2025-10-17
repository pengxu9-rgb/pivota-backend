import axios from 'axios';

const API_BASE_URL = 'https://web-production-fedb.up.railway.app';

const api = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
});

export interface MerchantRegistration {
  business_name: string;
  store_url: string;
  region: string;
  contact_email: string;
  contact_phone?: string;
  website?: string;
}

export interface RegistrationResponse {
  status: string;
  message: string;
  merchant_id: string;
  auto_approved: boolean;
  confidence_score: number;
  validation_details: any;
  full_kyb_deadline?: string;
  next_step: string;
}

export const onboardingApi = {
  // Register new merchant
  async register(data: MerchantRegistration): Promise<RegistrationResponse> {
    const response = await api.post('/merchant/onboarding/register', data);
    return response.data;
  },

  // Setup PSP connection
  async setupPSP(merchantId: string, pspType: string, apiKey: string) {
    const response = await api.post('/merchant/onboarding/psp/setup', {
      merchant_id: merchantId,
      psp_type: pspType,
      psp_key: apiKey,
    });
    return response.data;
  },

  // Upload KYC documents
  async uploadDocuments(merchantId: string, files: File[]) {
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
  async startShopifyOAuth(merchantId: string, shopDomain: string) {
    const params = new URLSearchParams({ merchant_id: merchantId, shop: shopDomain });
    const response = await api.get(`/integrations/shopify/oauth/start?${params.toString()}`);
    return response.data as { authorize: string };
  },
};

export default api;

