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
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

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
    return response.data;
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

export default api;

