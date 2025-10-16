/**
 * Merchant Onboarding Dashboard - Phase 2
 * Allows merchants to register, upload KYC, and connect PSP
 */

import React, { useState, useEffect } from 'react';
import { api } from '../services/api';

interface OnboardingStatus {
  merchant_id: string;
  business_name: string;
  kyc_status: string;
  psp_connected: boolean;
  psp_type?: string;
  api_key_issued: boolean;
  created_at: string;
  verified_at?: string;
}

type OnboardingStep = 'register' | 'kyc' | 'psp' | 'complete';

export const MerchantOnboardingDashboard: React.FC = () => {
  const [step, setStep] = useState<OnboardingStep>('register');
  const [merchantId, setMerchantId] = useState<string>('');
  const [apiKey, setApiKey] = useState<string>('');
  const [status, setStatus] = useState<OnboardingStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [resumeInput, setResumeInput] = useState('');

  const updateStep = (next: OnboardingStep) => {
    setStep(next);
    try {
      localStorage.setItem('merchant_onboarding_step', next);
    } catch {}
  };

  // Registration form
  const [businessName, setBusinessName] = useState('');
  const [storeUrl, setStoreUrl] = useState('');
  const [region, setRegion] = useState('US');
  const [contactEmail, setContactEmail] = useState('');
  const [contactPhone, setContactPhone] = useState('');

  // PSP setup form
  const [pspType, setPspType] = useState('stripe');
  const [pspSandboxKey, setPspSandboxKey] = useState('');

  // Load status if merchant_id exists in localStorage
  useEffect(() => {
    const savedMerchantId = localStorage.getItem('merchant_onboarding_id');
    const savedStep = localStorage.getItem('merchant_onboarding_step') as OnboardingStep | null;
    if (savedMerchantId) {
      setMerchantId(savedMerchantId);
      if (savedStep && savedStep !== 'complete') {
        // Optimistically show last step while status loads
        setStep(savedStep);
      }
      loadStatus(savedMerchantId);
    }
    const savedApiKey = localStorage.getItem('merchant_api_key');
    if (savedApiKey) setApiKey(savedApiKey);
  }, []);

  const loadStatus = async (id: string) => {
    try {
      const response = await api.get(`/merchant/onboarding/status/${id}`);
      setStatus(response.data);
      
      // Determine current step based on status
      if (response.data.psp_connected) {
        updateStep('complete');
      } else if (response.data.kyc_status === 'approved') {
        updateStep('psp');
      } else if (response.data.kyc_status === 'pending_verification') {
        updateStep('kyc');
      }
    } catch (err: any) {
      console.error('Failed to load status:', err);
      setError(err.response?.data?.detail || 'Failed to load status');
    }
  };

  const handleManualResume = async () => {
    const id = resumeInput.trim();
    if (!id) return;
    try {
      setError('');
      setMerchantId(id);
      localStorage.setItem('merchant_onboarding_id', id);
      await loadStatus(id);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to resume with this Merchant ID');
    }
  };

  const handleRegister = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError('');

    try {
      const response = await api.post('/merchant/onboarding/register', {
        business_name: businessName,
        store_url: storeUrl,
        region,
        contact_email: contactEmail,
        contact_phone: contactPhone,
      });

      const newMerchantId = response.data.merchant_id;
      setMerchantId(newMerchantId);
      localStorage.setItem('merchant_onboarding_id', newMerchantId);
      
      alert(`✅ Registration successful!\nMerchant ID: ${newMerchantId}\n\nKYC verification in progress (auto-approve in 5 seconds)...`);
      
      updateStep('kyc');
      
      // Poll for KYC approval
      setTimeout(() => loadStatus(newMerchantId), 6000);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Registration failed');
    } finally {
      setLoading(false);
    }
  };

  const handlePspSetup = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError('');

    try {
      const response = await api.post('/merchant/onboarding/psp/setup', {
        merchant_id: merchantId,
        psp_type: pspType,
        psp_sandbox_key: pspSandboxKey,
      });

      const key = response.data.api_key;
      setApiKey(key);
      localStorage.setItem('merchant_api_key', key);
      
      const validated = response.data.validated ? ' (Credentials Validated ✓)' : '';
      alert(`🎉 PSP Connected Successfully!${validated}\n\nYour API Key (save this!):\n${key}\n\nYou can now use /payment/execute with header:\nX-Merchant-API-Key: ${key}`);
      
      updateStep('complete');
      loadStatus(merchantId);
    } catch (err: any) {
      console.error('PSP Setup Error:', err);
      const errorDetail = err.response?.data?.detail || err.message || 'PSP setup failed';

      // Idempotent handling: if PSP already connected, proceed to completion
      if (typeof errorDetail === 'string' && errorDetail.toLowerCase().includes('already connected')) {
        alert('ℹ️ PSP already connected. Refreshing status...');
        await loadStatus(merchantId);
        setLoading(false);
        return;
      }
      const errorMsg = `❌ PSP Setup Failed:\n\n${errorDetail}\n\n${
        err.response?.status === 500 
          ? 'Server error - PSP validation may be temporarily unavailable. The backend is still deploying or there may be an issue with the PSP API.'
          : err.response?.status === 400
          ? 'This usually means the API key is invalid or KYC is not approved yet.'
          : 'Please check your API key and try again.'
      }`;
      setError(errorMsg);
      alert(errorMsg);
    } finally {
      setLoading(false);
    }
  };

  const handleRefreshStatus = () => {
    if (merchantId) {
      loadStatus(merchantId);
    }
  };

  return (
    <div className="max-w-4xl mx-auto p-6">
      <h1 className="text-3xl font-bold mb-6">Merchant Onboarding</h1>

      {/* Resume section */}
      <div className="card mb-6">
        <h2 className="text-xl font-semibold mb-3">Resume Onboarding</h2>
        {merchantId ? (
          <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-3">
            <div className="text-sm">
              <p><strong>Merchant ID:</strong> <span className="font-mono">{merchantId}</span></p>
              {apiKey && (
                <p className="text-gray-600"><strong>Saved API Key:</strong> present</p>
              )}
              {status && !status.psp_connected && (
                <p className="text-gray-600"><strong>Current Status:</strong> {status.kyc_status} • PSP Connected: {status.psp_connected ? 'Yes' : 'No'}</p>
              )}
            </div>
            <div className="flex gap-2">
              {!status?.psp_connected && (
                <button
                  onClick={() => updateStep(status?.kyc_status === 'approved' ? 'psp' : 'kyc')}
                  className="btn btn-primary"
                >
                  Continue
                </button>
              )}
              <button
                onClick={() => {
                  localStorage.removeItem('merchant_onboarding_id');
                  localStorage.removeItem('merchant_onboarding_step');
                  setMerchantId('');
                  setStatus(null);
                  updateStep('register');
                }}
                className="btn btn-secondary"
              >
                Switch / Start Over
              </button>
            </div>
          </div>
        ) : (
          <div className="flex flex-col md:flex-row md:items-end gap-3">
            <div className="flex-1">
              <label className="block text-sm font-medium mb-1">Have a Merchant ID?</label>
              <input
                type="text"
                value={resumeInput}
                onChange={(e) => setResumeInput(e.target.value)}
                className="w-full px-3 py-2 border rounded font-mono"
                placeholder="merch_..."
              />
            </div>
            <button onClick={handleManualResume} className="btn btn-primary md:w-auto">
              Resume
            </button>
          </div>
        )}
      </div>

      {/* Progress Indicator */}
      <div className="mb-8">
        <div className="flex items-center justify-between">
          <div className={`flex-1 text-center ${step === 'register' ? 'font-bold text-blue-600' : 'text-gray-400'}`}>
            <div className={`w-10 h-10 mx-auto rounded-full flex items-center justify-center mb-2 ${step === 'register' ? 'bg-blue-600 text-white' : 'bg-gray-300'}`}>1</div>
            Register
          </div>
          <div className={`flex-1 text-center ${step === 'kyc' ? 'font-bold text-blue-600' : 'text-gray-400'}`}>
            <div className={`w-10 h-10 mx-auto rounded-full flex items-center justify-center mb-2 ${step === 'kyc' ? 'bg-blue-600 text-white' : 'bg-gray-300'}`}>2</div>
            KYC Verification
          </div>
          <div className={`flex-1 text-center ${step === 'psp' ? 'font-bold text-blue-600' : 'text-gray-400'}`}>
            <div className={`w-10 h-10 mx-auto rounded-full flex items-center justify-center mb-2 ${step === 'psp' ? 'bg-blue-600 text-white' : 'bg-gray-300'}`}>3</div>
            PSP Setup
          </div>
          <div className={`flex-1 text-center ${step === 'complete' ? 'font-bold text-green-600' : 'text-gray-400'}`}>
            <div className={`w-10 h-10 mx-auto rounded-full flex items-center justify-center mb-2 ${step === 'complete' ? 'bg-green-600 text-white' : 'bg-gray-300'}`}>✓</div>
            Complete
          </div>
        </div>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded mb-4">
          {error}
        </div>
      )}

      {/* Step 1: Register */}
      {step === 'register' && (
        <div className="card">
          <h2 className="text-2xl font-semibold mb-4">Step 1: Business Registration</h2>
          <form onSubmit={handleRegister} className="space-y-4">
            <div>
              <label className="block text-sm font-medium mb-1">Business Name *</label>
              <input
                type="text"
                required
                value={businessName}
                onChange={(e) => setBusinessName(e.target.value)}
                className="w-full px-3 py-2 border rounded"
                placeholder="Acme Inc."
              />
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">Store URL *</label>
              <input
                type="url"
                required
                value={storeUrl}
                onChange={(e) => setStoreUrl(e.target.value)}
                className="w-full px-3 py-2 border rounded"
                placeholder="https://mystore.shopify.com or https://mystore.com"
              />
              <p className="text-xs text-gray-500 mt-1">Required for KYB verification and MCP integration</p>
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">Region *</label>
              <select
                value={region}
                onChange={(e) => setRegion(e.target.value)}
                className="w-full px-3 py-2 border rounded"
              >
                <option value="US">United States</option>
                <option value="EU">European Union</option>
                <option value="APAC">Asia Pacific</option>
                <option value="UK">United Kingdom</option>
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">Contact Email *</label>
              <input
                type="email"
                required
                value={contactEmail}
                onChange={(e) => setContactEmail(e.target.value)}
                className="w-full px-3 py-2 border rounded"
                placeholder="contact@example.com"
              />
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">Contact Phone</label>
              <input
                type="tel"
                value={contactPhone}
                onChange={(e) => setContactPhone(e.target.value)}
                className="w-full px-3 py-2 border rounded"
                placeholder="+1 (555) 123-4567"
              />
            </div>
            <button
              type="submit"
              disabled={loading}
              className="btn btn-primary w-full"
            >
              {loading ? 'Registering...' : 'Register Business'}
            </button>
          </form>
        </div>
      )}

      {/* Step 2: KYC Verification */}
      {step === 'kyc' && (
        <div className="card">
          <h2 className="text-2xl font-semibold mb-4">Step 2: KYC Verification</h2>
          <div className="bg-blue-50 border border-blue-200 p-4 rounded mb-4">
            <p className="font-medium">Merchant ID: {merchantId}</p>
            <p className="text-sm text-gray-600 mt-1">Status: {status?.kyc_status || 'pending_verification'}</p>
          </div>
          
          {status?.kyc_status === 'pending_verification' && (
            <div className="text-center py-8">
              <div className="inline-block animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600 mb-4"></div>
              <p className="text-gray-600">KYC verification in progress...</p>
              <p className="text-sm text-gray-500 mt-2">Auto-approval simulation (5 seconds)</p>
              <button
                onClick={handleRefreshStatus}
                className="btn btn-secondary mt-4"
              >
                Refresh Status
              </button>
            </div>
          )}
          
          {status?.kyc_status === 'approved' && (
            <div className="text-center py-8">
              <div className="text-green-600 text-5xl mb-4">✓</div>
              <p className="text-xl font-semibold text-green-600">KYC Approved!</p>
              <button
                onClick={() => updateStep('psp')}
                className="btn btn-primary mt-4"
              >
                Continue to PSP Setup
              </button>
            </div>
          )}
          
          {status?.kyc_status === 'rejected' && (
            <div className="text-center py-8">
              <div className="text-red-600 text-5xl mb-4">✗</div>
              <p className="text-xl font-semibold text-red-600">KYC Rejected</p>
              <p className="text-gray-600 mt-2">Please contact support</p>
            </div>
          )}
        </div>
      )}

      {/* Step 3: PSP Setup */}
      {step === 'psp' && (
        <div className="card">
          <h2 className="text-2xl font-semibold mb-4">Step 3: Connect Payment Provider</h2>
          <form onSubmit={handlePspSetup} className="space-y-4">
            <div>
              <label className="block text-sm font-medium mb-1">PSP Type *</label>
              <select
                value={pspType}
                onChange={(e) => setPspType(e.target.value)}
                className="w-full px-3 py-2 border rounded"
              >
                <option value="stripe">Stripe</option>
                <option value="adyen">Adyen</option>
                <option value="shoppay">ShopPay</option>
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">
                {pspType === 'stripe' ? 'Stripe Test Key' : pspType === 'adyen' ? 'Adyen API Key' : 'ShopPay API Key'} *
              </label>
              <input
                type="password"
                required
                value={pspSandboxKey}
                onChange={(e) => setPspSandboxKey(e.target.value)}
                className="w-full px-3 py-2 border rounded font-mono"
                placeholder={pspType === 'stripe' ? 'sk_test_...' : 'Enter your API key'}
              />
              <p className="text-sm text-gray-500 mt-1">
                Use your {pspType} test/sandbox API key
              </p>
            </div>
            <button
              type="submit"
              disabled={loading}
              className="btn btn-primary w-full"
            >
              {loading ? 'Connecting...' : 'Connect PSP & Get API Key'}
            </button>
          </form>
        </div>
      )}

      {/* Step 4: Complete */}
      {step === 'complete' && (
        <div className="card">
          <h2 className="text-2xl font-semibold mb-4">🎉 Onboarding Complete!</h2>
          
          <div className="bg-green-50 border border-green-200 p-4 rounded mb-4">
            <p className="font-semibold text-green-800">Your merchant account is ready!</p>
            <div className="mt-3 space-y-2 text-sm">
              <p><strong>Merchant ID:</strong> {merchantId}</p>
              <p><strong>PSP:</strong> {status?.psp_type}</p>
              <p><strong>API Key:</strong> {apiKey || '(saved in localStorage)'}</p>
            </div>
          </div>

          <div className="bg-blue-50 border border-blue-200 p-4 rounded mb-4">
            <h3 className="font-semibold mb-2">Next Steps:</h3>
            <ol className="list-decimal list-inside space-y-1 text-sm">
              <li>Save your API key securely (shown only once)</li>
              <li>Use the API key in payment requests: <code className="bg-gray-100 px-1 py-0.5 rounded">X-Merchant-API-Key: {apiKey || 'your-key'}</code></li>
              <li>Make payments via <code className="bg-gray-100 px-1 py-0.5 rounded">POST /payment/execute</code></li>
              <li>View transactions in the merchant dashboard</li>
            </ol>
          </div>

          <div className="flex gap-3">
            <button
              onClick={() => window.location.href = '/admin'}
              className="btn btn-primary flex-1"
            >
              Go to Dashboard
            </button>
            <button
              onClick={() => {
                localStorage.removeItem('merchant_onboarding_id');
                localStorage.removeItem('merchant_onboarding_step');
                updateStep('register');
                setMerchantId('');
                setApiKey('');
                setStatus(null);
              }}
              className="btn btn-secondary flex-1"
            >
              Start New Onboarding
            </button>
          </div>
        </div>
      )}

      {/* Current Status Card (always visible if merchant_id exists) */}
      {merchantId && (
        <div className="card mt-6 bg-gray-50">
          <h3 className="text-lg font-semibold mb-3">Current Status</h3>
          <div className="text-sm space-y-1">
            <p><strong>Merchant ID:</strong> {merchantId}</p>
            <p><strong>Business:</strong> {status?.business_name}</p>
            <p><strong>KYC Status:</strong> 
              <span className={`ml-2 px-2 py-1 rounded text-xs ${
                status?.kyc_status === 'approved' ? 'bg-green-100 text-green-800' :
                status?.kyc_status === 'rejected' ? 'bg-red-100 text-red-800' :
                'bg-yellow-100 text-yellow-800'
              }`}>
                {status?.kyc_status}
              </span>
            </p>
            <p><strong>PSP Connected:</strong> {status?.psp_connected ? `✓ ${status.psp_type}` : '✗ Not connected'}</p>
            <p><strong>API Key Issued:</strong> {status?.api_key_issued ? '✓ Yes' : '✗ No'}</p>
          </div>
          <button
            onClick={handleRefreshStatus}
            className="btn btn-secondary btn-sm mt-3"
          >
            Refresh Status
          </button>
        </div>
      )}
    </div>
  );
};

