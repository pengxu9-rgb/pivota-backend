import { useState } from 'react';
import { Loader, CheckCircle, AlertCircle, CreditCard, Plus } from 'lucide-react';
import { onboardingApi } from '../lib/api';

interface PSPSetupStepProps {
  merchantId: string;
  onComplete: (data: any) => void;
}

type PSPType = 'stripe' | 'adyen' | 'paypal' | 'checkout' | 'other' | '';

export default function PSPSetupStep({ merchantId, onComplete }: PSPSetupStepProps) {
  const [pspType, setPspType] = useState<PSPType>('');
  const [customPspName, setCustomPspName] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [secretKey, setSecretKey] = useState(''); // For PayPal
  const [accountId, setAccountId] = useState(''); // For Checkout.com
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    
    if (!pspType) {
      setError('Please select a payment provider');
      return;
    }

    if (pspType === 'other' && !customPspName.trim()) {
      setError('Please enter the PSP name');
      return;
    }

    if (pspType === 'paypal' && !secretKey.trim()) {
      setError('PayPal requires both Client ID and Client Secret');
      return;
    }

    if (pspType === 'adyen' && !accountId.trim()) {
      setError('Adyen requires Merchant Account');
      return;
    }

    if (pspType === 'checkout' && !accountId.trim()) {
      setError('Checkout.com requires Processing Channel ID');
      return;
    }

    setLoading(true);
    setError('');

    try {
      const payload: any = {
        provider: pspType === 'other' ? customPspName.toLowerCase().replace(/\s+/g, '_') : pspType,
        api_key: apiKey
      };

      if (pspType === 'paypal' && secretKey) {
        payload.secret_key = secretKey;
      }

      if ((pspType === 'adyen' || pspType === 'checkout') && accountId) {
        payload.account_id = accountId;
      }

      if (pspType === 'other') {
        payload.custom_psp = true;
        payload.setup_later = true; // Mark for later setup
      }

      await onboardingApi.setupPSP(merchantId, payload.provider, apiKey, payload);
      
      onComplete({
        psp_type: payload.provider,
        api_key: apiKey,
        custom_psp: pspType === 'other'
      });
    } catch (err: any) {
      console.error('PSP setup error:', err);
      
      const errorMsg = err.response?.data?.detail || err.message || 'PSP setup failed. Please check your credentials.';
      
      if (err.response?.status === 422 && err.response?.data?.detail) {
        const validationErrors = err.response.data.detail;
        if (Array.isArray(validationErrors)) {
          setError(validationErrors.map((e: any) => e.msg).join(', '));
        } else {
          setError(String(validationErrors));
        }
      } else {
        setError(String(errorMsg));
      }
    } finally {
      setLoading(false);
    }
  };

  const pspOptions = [
    { id: 'stripe', name: 'Stripe', icon: '💳', color: 'indigo', description: 'Popular choice' },
    { id: 'adyen', name: 'Adyen', icon: '🌐', color: 'green', description: 'Enterprise grade' },
    { id: 'paypal', name: 'PayPal', icon: '💰', color: 'blue', description: 'Trusted worldwide' },
    { id: 'checkout', name: 'Checkout.com', icon: '✓', color: 'purple', description: 'Global payments' },
  ];

  const getFieldLabels = () => {
    switch (pspType) {
      case 'stripe':
        return { apiKeyLabel: 'Secret Key', apiKeyPlaceholder: 'sk_test_... or sk_live_...' };
      case 'adyen':
        return { apiKeyLabel: 'API Key', apiKeyPlaceholder: 'Your Adyen API key' };
      case 'paypal':
        return { apiKeyLabel: 'Client ID', apiKeyPlaceholder: 'Your PayPal Client ID' };
      case 'checkout':
        return { apiKeyLabel: 'Secret Key', apiKeyPlaceholder: 'sk_...' };
      case 'other':
        return { apiKeyLabel: 'API Key', apiKeyPlaceholder: 'Enter API key (can setup later)' };
      default:
        return { apiKeyLabel: 'API Key', apiKeyPlaceholder: '' };
    }
  };

  const labels = getFieldLabels();

  return (
    <div>
      <div className="mb-6">
        <h2 className="text-2xl font-bold text-slate-900">Payment Setup</h2>
        <p className="text-sm text-slate-600 mt-1">
          Connect your payment provider to start accepting payments
        </p>
      </div>

      <form onSubmit={handleSubmit} className="space-y-6">
        {/* PSP Selection */}
        <div>
          <label className="block text-sm font-medium text-slate-700 mb-3">
            Select Payment Provider <span className="text-red-500">*</span>
          </label>
          <div className="grid grid-cols-2 gap-3">
            {pspOptions.map((psp) => (
              <button
                key={psp.id}
                type="button"
                onClick={() => {
                  setPspType(psp.id as PSPType);
                  setCustomPspName('');
                }}
                className={`p-4 border-2 rounded-lg text-left transition-all ${
                  pspType === psp.id
                    ? 'border-blue-500 bg-blue-50 ring-2 ring-blue-200'
                    : 'border-slate-200 hover:border-slate-300 hover:bg-slate-50'
                }`}
              >
                <div className="flex items-center gap-3">
                  <div className={`w-12 h-12 bg-${psp.color}-100 rounded-lg flex items-center justify-center text-2xl`}>
                    {psp.icon}
                  </div>
                  <div>
                    <div className="font-semibold text-slate-900">{psp.name}</div>
                    <div className="text-xs text-slate-500">{psp.description}</div>
                  </div>
                </div>
              </button>
            ))}

            {/* Other PSP Option */}
            <button
              type="button"
              onClick={() => setPspType('other')}
              className={`p-4 border-2 rounded-lg text-left transition-all col-span-2 ${
                pspType === 'other'
                  ? 'border-blue-500 bg-blue-50 ring-2 ring-blue-200'
                  : 'border-slate-200 hover:border-slate-300 hover:bg-slate-50 border-dashed'
              }`}
            >
              <div className="flex items-center gap-3">
                <div className="w-12 h-12 bg-gray-100 rounded-lg flex items-center justify-center">
                  <Plus className="w-6 h-6 text-gray-600" />
                </div>
                <div>
                  <div className="font-semibold text-slate-900">Other Payment Provider</div>
                  <div className="text-xs text-slate-500">Use a different PSP or setup later</div>
                </div>
              </div>
            </button>
          </div>
        </div>

        {/* Custom PSP Name Input */}
        {pspType === 'other' && (
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">
              PSP Name <span className="text-red-500">*</span>
            </label>
            <input
              type="text"
              required
              value={customPspName}
              onChange={(e) => setCustomPspName(e.target.value)}
              className="w-full px-4 py-2 border border-slate-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
              placeholder="e.g., Mollie, Square, Braintree"
            />
            <p className="text-xs text-slate-500 mt-1">
              Enter the name of your payment provider
            </p>
          </div>
        )}

        {/* API Key Input */}
        {pspType && (
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">
              {labels.apiKeyLabel}{' '}
              {pspType === 'other' ? (
                <span className="text-slate-500 font-normal">(Optional - can setup later)</span>
              ) : (
                <span className="text-red-500">*</span>
              )}
            </label>
            <input
              type="password"
              required={pspType !== 'other'}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              className="w-full px-4 py-2 border border-slate-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent font-mono text-sm"
              placeholder={labels.apiKeyPlaceholder}
            />
            {pspType !== 'other' && (
              <p className="text-xs text-slate-500 mt-1">
                {pspType === 'stripe' && 'Find this in Stripe Dashboard > Developers > API keys'}
                {pspType === 'adyen' && 'Find this in Adyen Customer Area > API Credentials'}
                {pspType === 'paypal' && 'Find this in PayPal Developer Dashboard'}
                {pspType === 'checkout' && 'Find this in Checkout.com Hub > Settings > Channels'}
              </p>
            )}
          </div>
        )}

        {/* Adyen Merchant Account */}
        {pspType === 'adyen' && (
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">
              Merchant Account <span className="text-red-500">*</span>
            </label>
            <input
              type="text"
              required
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
              className="w-full px-4 py-2 border border-slate-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent font-mono text-sm"
              placeholder="YourCompanyECOM"
            />
            <p className="text-xs text-slate-500 mt-1">
              Find this in Adyen Customer Area under Account settings
            </p>
          </div>
        )}

        {/* PayPal Client Secret */}
        {pspType === 'paypal' && (
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">
              Client Secret <span className="text-red-500">*</span>
            </label>
            <input
              type="password"
              required
              value={secretKey}
              onChange={(e) => setSecretKey(e.target.value)}
              className="w-full px-4 py-2 border border-slate-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent font-mono text-sm"
              placeholder="Your PayPal Client Secret"
            />
          </div>
        )}

        {/* Checkout.com Processing Channel ID */}
        {pspType === 'checkout' && (
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">
              Processing Channel ID <span className="text-red-500">*</span>
            </label>
            <input
              type="text"
              required
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
              className="w-full px-4 py-2 border border-slate-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent font-mono text-sm"
              placeholder="pc_..."
            />
            <p className="text-xs text-slate-500 mt-1">
              Required for Checkout.com - find this in your Hub dashboard
            </p>
          </div>
        )}

        {/* Info Box for Other PSP */}
        {pspType === 'other' && (
          <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
            <div className="flex items-start gap-2">
              <AlertCircle className="w-5 h-5 text-blue-600 mt-0.5 flex-shrink-0" />
              <div className="text-sm text-blue-800">
                <p className="font-medium mb-1">Custom PSP Setup</p>
                <p>You can add the PSP name now and configure the API keys later in your dashboard under PSP Settings.</p>
              </div>
            </div>
          </div>
        )}

        {/* Error Message */}
        {error && (
          <div className="flex items-center gap-2 p-4 bg-red-50 border border-red-200 rounded-lg">
            <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0" />
            <p className="text-sm text-red-700">{error}</p>
          </div>
        )}

        {/* Submit Button */}
        <button
          type="submit"
          disabled={loading || !pspType}
          className="w-full py-3 px-4 bg-gradient-to-r from-blue-500 to-indigo-600 text-white font-medium rounded-lg hover:from-blue-600 hover:to-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
        >
          {loading ? (
            <>
              <Loader className="w-5 h-5 animate-spin" />
              Connecting...
            </>
          ) : (
            <>
              <CheckCircle className="w-5 h-5" />
              {pspType === 'other' ? 'Save & Continue' : 'Connect Payment Provider'}
            </>
          )}
        </button>

        <p className="text-xs text-center text-slate-500">
          {pspType === 'other' 
            ? 'You can configure API keys later in your dashboard'
            : 'Your API keys are encrypted and stored securely'}
        </p>
      </form>
    </div>
  );
}
