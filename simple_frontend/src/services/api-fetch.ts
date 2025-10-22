// Alternative API service using native fetch instead of axios
// Use this if axios has timeout issues

const API_BASE_URL = 'https://web-production-fedb.up.railway.app';

// Helper function to make fetch requests with timeout
async function fetchWithTimeout(url: string, options: RequestInit = {}, timeout: number = 30000) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeout);
  
  try {
    const response = await fetch(url, {
      ...options,
      signal: controller.signal,
    });
    clearTimeout(timeoutId);
    return response;
  } catch (error) {
    clearTimeout(timeoutId);
    throw error;
  }
}

// Auth API using fetch
export const authApiFetch = {
  signin: async (email: string, password: string) => {
    console.log('🔐 [FETCH] Attempting signin:', { email, url: `${API_BASE_URL}/auth/signin` });
    
    try {
      const response = await fetchWithTimeout(`${API_BASE_URL}/auth/signin`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ email, password }),
      }, 30000); // 30 second timeout

      console.log('📥 [FETCH] Response status:', response.status);
      
      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || 'Signin failed');
      }

      const data = await response.json();
      console.log('✅ [FETCH] Signin successful:', data);
      return data;
    } catch (error: any) {
      console.error('❌ [FETCH] Signin error:', error);
      throw error;
    }
  },

  signup: async (email: string, password: string, role: string = 'employee') => {
    console.log('📝 [FETCH] Attempting signup:', { email });
    
    const response = await fetchWithTimeout(`${API_BASE_URL}/auth/signup`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ email, password, role }),
    });

    if (!response.ok) {
      const errorData = await response.json();
      throw new Error(errorData.detail || 'Signup failed');
    }

    return response.json();
  },

  me: async () => {
    const token = localStorage.getItem('auth_token');
    if (!token) {
      throw new Error('No token found');
    }

    const response = await fetchWithTimeout(`${API_BASE_URL}/auth/me`, {
      method: 'GET',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${token}`,
      },
    });

    if (!response.ok) {
      throw new Error('Failed to get user info');
    }

    const data = await response.json();
    return data.user || data;
  },

  getUsers: async () => {
    const token = localStorage.getItem('auth_token');
    const response = await fetchWithTimeout(`${API_BASE_URL}/auth/admin/users`, {
      headers: {
        'Authorization': `Bearer ${token}`,
      },
    });
    return response.json();
  },

  approveUser: async (userId: string, approved: boolean) => {
    const token = localStorage.getItem('auth_token');
    const response = await fetchWithTimeout(`${API_BASE_URL}/auth/admin/users/${userId}/approve`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${token}`,
      },
      body: JSON.stringify({ approved }),
    });
    return response.json();
  },

  changeUserRole: async (userId: string, role: string) => {
    const token = localStorage.getItem('auth_token');
    const response = await fetchWithTimeout(`${API_BASE_URL}/auth/admin/users/${userId}/role`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${token}`,
      },
      body: JSON.stringify({ role }),
    });
    return response.json();
  },
};

// Admin API using fetch
export const adminApiFetch = {
  getPSPStatus: async () => {
    const token = localStorage.getItem('auth_token');
    const response = await fetchWithTimeout(`${API_BASE_URL}/admin/psp/status`, {
      headers: {
        'Authorization': `Bearer ${token}`,
      },
    });
    return response.json();
  },

  testPSP: async (pspId: string) => {
    const token = localStorage.getItem('auth_token');
    const response = await fetchWithTimeout(`${API_BASE_URL}/admin/psp/${pspId}/test`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${token}`,
      },
    });
    return response.json();
  },

  getAnalytics: async (days: number = 30) => {
    const token = localStorage.getItem('auth_token');
    const response = await fetchWithTimeout(`${API_BASE_URL}/admin/analytics/overview?days=${days}`, {
      headers: {
        'Authorization': `Bearer ${token}`,
      },
    });
    return response.json();
  },
};


