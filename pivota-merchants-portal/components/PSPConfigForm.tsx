import React, { useState } from 'react';
import { X } from 'lucide-react';

interface PSPConfigFormProps {
  provider: string;
  merchantId: string;
  onClose: () => void;
  onSuccess: () => void;
  apiClient: any;
}

export function PSPConfigForm({ provider, merchantId, onClose, onSuccess, apiClient }: PSPConfigFormProps) {
  const [apiKey, setApiKey] = useState('');
  const [secretKey, setSecretKey] = useState('');
  const [accountId, setAccountId] = useState('');
  const [saving, setSaving] = useState(false);
  
  // Normalize provider name: "Checkout.com" → "checkout", "PayPal" → "paypal"
  const providerLower = provider.toLowerCase().replace('.com', '').replace('.', '');
  
  // Validation based on provider requirements (matching Employee Portal logic)
  const canSave = provider && merchantId && apiKey && 
    (providerLower !== 'paypal' || secretKey) &&
    (providerLower !== 'braintree' || secretKey) &&
    (providerLower !== 'checkout' || accountId) &&
    (providerLower !== 'square' || accountId) &&
    (providerLower !== 'braintree' || accountId) &&
    !saving;

  const getFieldLabels = () => {
    switch (providerLower) {
      case 'paypal':
        return {
          apiKeyLabel: 'Client ID',
          apiKeyPlaceholder: 'Enter your PayPal Client ID',
          showSecretKey: true,
          secretKeyLabel: 'Client Secret',
          secretKeyPlaceholder: 'Enter your PayPal Client Secret',
          accountIdLabel: 'Account ID (optional)',
          accountIdPlaceholder: 'Optional account identifier',
          accountIdRequired: false
        };
      case 'stripe':
        return {
          apiKeyLabel: 'Secret Key',
          apiKeyPlaceholder: 'sk_live_... or sk_test_...',
          showSecretKey: false,
          secretKeyLabel: '',
          secretKeyPlaceholder: '',
          accountIdLabel: 'Account ID (optional)',
          accountIdPlaceholder: 'acct_... (for connected accounts)',
          accountIdRequired: false
        };
      case 'adyen':
        return {
          apiKeyLabel: 'API Key',
          apiKeyPlaceholder: 'AQE... (Enter your Adyen API key)',
          showSecretKey: false,
          secretKeyLabel: '',
          secretKeyPlaceholder: '',
          accountIdLabel: 'Merchant Account (optional)',
          accountIdPlaceholder: 'Your Adyen merchant account',
          accountIdRequired: false
        };
      case 'checkout':
        return {
          apiKeyLabel: 'Secret Key',
          apiKeyPlaceholder: 'sk_... (your secret key)',
          showSecretKey: false,
          secretKeyLabel: '',
          secretKeyPlaceholder: '',
          accountIdLabel: 'Processing Channel ID',
          accountIdPlaceholder: 'pc_... (required for Checkout.com)',
          accountIdRequired: true
        };
      case 'square':
        return {
          apiKeyLabel: 'Access Token',
          apiKeyPlaceholder: 'EAAAE... (your Square access token)',
          showSecretKey: false,
          secretKeyLabel: '',
          secretKeyPlaceholder: '',
          accountIdLabel: 'Location ID',
          accountIdPlaceholder: 'L... (find in Square Dashboard → Locations)',
          accountIdRequired: true
        };
      case 'mollie':
        return {
          apiKeyLabel: 'API Key',
          apiKeyPlaceholder: 'test_... or live_...',
          showSecretKey: false,
          secretKeyLabel: '',
          secretKeyPlaceholder: '',
          accountIdLabel: 'Profile ID (optional)',
          accountIdPlaceholder: 'pfl_... (optional)',
          accountIdRequired: false
        };
      case 'braintree':
        return {
          apiKeyLabel: 'Public Key',
          apiKeyPlaceholder: 'Enter your Braintree public key',
          showSecretKey: true,
          secretKeyLabel: 'Private Key',
          secretKeyPlaceholder: 'Enter your Braintree private key',
          accountIdLabel: 'Merchant ID',
          accountIdPlaceholder: 'Your Braintree merchant ID',
          accountIdRequired: true
        };
      default:
        return {
          apiKeyLabel: 'API Key',
          apiKeyPlaceholder: 'Enter your API key',
          showSecretKey: false,
          secretKeyLabel: '',
          secretKeyPlaceholder: '',
          accountIdLabel: 'Account ID (optional)',
          accountIdPlaceholder: 'Optional account identifier',
          accountIdRequired: false
        };
    }
  };

  const labels = getFieldLabels();

  const handleSave = async () => {
    if (!canSave) {
      alert('Please fill in all required fields');
      return;
    }
    
    try {
      setSaving(true);
      
      // Build payload matching Employee Portal format
      const payload: any = {
        provider: providerLower,
        merchant_id: merchantId,
        api_key: apiKey,
        account_id: accountId || undefined
      };
      
      // Add secret_key for PayPal and Braintree
      if ((providerLower === 'paypal' || providerLower === 'braintree') && secretKey) {
        payload.secret_key = secretKey;
      }
      
      console.log('💾 Saving PSP configuration:', { provider: providerLower, merchant_id: merchantId, has_account_id: !!accountId, has_secret: !!secretKey });
      
      const response = await apiClient.connectPSPProvider(payload);
      console.log('✅ PSP connected:', response);
      
      alert(`✅ ${provider} connected successfully!`);
      onSuccess();
      onClose();
    } catch (error: any) {
      console.error('❌ PSP connection error:', error);
      alert(`❌ Failed to connect ${provider}: ${error.response?.data?.detail || error.message}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
      <div className="bg-white rounded-lg p-6 max-w-md w-full max-h-[80vh] overflow-y-auto">
        <div className="flex justify-between items-center mb-4">
          <h3 className="text-lg font-semibold">Configure {provider}</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600">
            <X className="w-5 h-5" />
          </button>
        </div>
        
        <div className="space-y-4">
          {/* Merchant ID */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Merchant ID <span className="text-red-500">*</span>
            </label>
            <input
              type="text"
              value={merchantId}
              disabled
              className="w-full px-3 py-2 border rounded-lg bg-gray-50 text-gray-600"
            />
            <p className="text-xs text-gray-500 mt-1">Your merchant ID (auto-filled)</p>
          </div>

          {/* API Key / Access Token / Client ID / Public Key */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              {labels.apiKeyLabel} <span className="text-red-500">*</span>
            </label>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={labels.apiKeyPlaceholder}
              className="w-full px-3 py-2 border rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            />
            {providerLower === 'stripe' && (
              <p className="text-xs text-gray-500 mt-1">
                Use sk_test_... for testing, sk_live_... for production
              </p>
            )}
          </div>
          
          {/* Secret Key / Client Secret / Private Key (PayPal and Braintree) */}
          {labels.showSecretKey && (
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-2">
                {labels.secretKeyLabel} <span className="text-red-500">*</span>
              </label>
              <input
                type="password"
                value={secretKey}
                onChange={(e) => setSecretKey(e.target.value)}
                placeholder={labels.secretKeyPlaceholder}
                className="w-full px-3 py-2 border rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
              />
            </div>
          )}
          
          {/* Account ID / Location ID / Merchant ID / Processing Channel ID */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              {labels.accountIdLabel}
              {labels.accountIdRequired && <span className="text-red-500"> *</span>}
            </label>
            <input
              type="text"
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
              placeholder={labels.accountIdPlaceholder}
              className="w-full px-3 py-2 border rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            />
            {providerLower === 'checkout' && (
              <p className="text-xs text-gray-500 mt-1">
                Required - Find this in your Checkout.com dashboard
              </p>
            )}
            {providerLower === 'square' && (
              <p className="text-xs text-gray-500 mt-1">
                Required - Find in Square Dashboard → Locations
              </p>
            )}
            {providerLower === 'braintree' && (
              <p className="text-xs text-gray-500 mt-1">
                Required - Your Braintree merchant account ID
              </p>
            )}
          </div>
          
          {/* Info box */}
          <div className="bg-blue-50 border border-blue-200 rounded-lg p-3">
            <p className="text-sm text-blue-800">
              {providerLower === 'stripe' && '💡 Find your API keys in Stripe Dashboard → Developers → API keys'}
              {providerLower === 'paypal' && '💡 Get Client ID and Secret from PayPal Developer Dashboard'}
              {providerLower === 'adyen' && '💡 Generate API keys in Adyen Customer Area → API Credentials'}
              {providerLower === 'checkout' && '💡 Find keys and Processing Channel ID in Checkout.com Hub'}
              {providerLower === 'square' && '💡 Get Access Token from Square Dashboard → Developer → Access Tokens. Location ID under Locations.'}
              {providerLower === 'mollie' && '💡 Generate keys in Mollie Dashboard → Developers → API keys (test_... for testing, live_... for production)'}
              {providerLower === 'braintree' && '💡 Find keys in Braintree Control Panel → Settings → API Keys (Public Key, Private Key, Merchant ID)'}
            </p>
          </div>
        </div>
        
        <div className="flex space-x-3 mt-6">
          <button
            onClick={handleSave}
            disabled={!canSave}
            className="flex-1 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed font-medium"
          >
            {saving ? 'Connecting...' : `Connect ${provider}`}
          </button>
          <button
            onClick={onClose}
            className="px-4 py-2 border border-gray-300 rounded-lg hover:bg-gray-50 font-medium"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}