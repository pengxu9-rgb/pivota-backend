import { useState } from 'react';
import { Loader, CheckCircle, AlertCircle, CreditCard } from 'lucide-react';
import { onboardingApi } from '../lib/api';

interface PSPSetupStepProps {
  merchantId: string;
  onComplete: (data: any) => void;
}

export default function PSPSetupStep({ merchantId, onComplete }: PSPSetupStepProps) {
  const [pspType, setPspType] = useState<'stripe' | 'adyen' | ''>('');
  const [apiKey, setApiKey] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!pspType) {
      setError('Please select a payment provider');
      return;
    }

    setLoading(true);
    setError('');

    try {
      await onboardingApi.setupPSP(merchantId, pspType, apiKey);
      
      onComplete({
        psp_type: pspType,
        api_key: apiKey,
      });
    } catch (err: any) {
      console.error('PSP setup error:', err);
      console.error('Error response:', err.response);
      console.error('Error data:', err.response?.data);
      
      const errorMsg = err.response?.data?.detail || err.message || 'PSP setup failed. Please check your API key.';
      
      // Handle validation errors (422)
      if (err.response?.status === 422 && err.response?.data?.detail) {
        const validationErrors = err.response.data.detail;
        if (Array.isArray(validationErrors)) {
          setError(validationErrors.map((e: any) => e.msg).join(', '));
        } else if (typeof validationErrors === 'string') {
          setError(validationErrors);
        } else {
          setError(JSON.stringify(validationErrors));
        }
      } 
      // Handle 400 errors (business logic)
      else if (err.response?.status === 400) {
        if (typeof errorMsg === 'string') {
          setError(errorMsg);
        } else {
          setError(`Error: ${JSON.stringify(errorMsg)}`);
        }
      }
      // Handle other errors
      else if (typeof errorMsg === 'string') {
        setError(errorMsg);
      } else {
        setError('PSP setup failed. Please try again.');
      }
    } finally {
      setLoading(false);
    }
  };

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
          <div className="grid grid-cols-2 gap-4">
            {/* Stripe Option */}
            <button
              type="button"
              onClick={() => setPspType('stripe')}
              className={`p-4 border-2 rounded-lg text-left transition-all ${
                pspType === 'stripe'
                  ? 'border-blue-500 bg-blue-50'
                  : 'border-slate-200 hover:border-slate-300'
              }`}
            >
              <div className="flex items-center gap-3">
                <div className="w-12 h-12 bg-indigo-100 rounded-lg flex items-center justify-center">
                  <CreditCard className="w-6 h-6 text-indigo-600" />
                </div>
                <div>
                  <div className="font-semibold text-slate-900">Stripe</div>
                  <div className="text-xs text-slate-500">Popular choice</div>
                </div>
              </div>
            </button>

            {/* Adyen Option */}
            <button
              type="button"
              onClick={() => setPspType('adyen')}
              className={`p-4 border-2 rounded-lg text-left transition-all ${
                pspType === 'adyen'
                  ? 'border-blue-500 bg-blue-50'
                  : 'border-slate-200 hover:border-slate-300'
              }`}
            >
              <div className="flex items-center gap-3">
                <div className="w-12 h-12 bg-green-100 rounded-lg flex items-center justify-center">
                  <CreditCard className="w-6 h-6 text-green-600" />
                </div>
                <div>
                  <div className="font-semibold text-slate-900">Adyen</div>
                  <div className="text-xs text-slate-500">Enterprise grade</div>
                </div>
              </div>
            </button>
          </div>
        </div>

        {/* API Key Input */}
        {pspType && (
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">
              {pspType === 'stripe' ? 'Stripe Secret Key' : 'Adyen API Key'}{' '}
              <span className="text-red-500">*</span>
            </label>
            <input
              type="password"
              required
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              className="w-full px-4 py-2 border border-slate-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent font-mono text-sm"
              placeholder={
                pspType === 'stripe'
                  ? 'sk_test_...'
                  : 'AQE...'
              }
            />
            <p className="text-xs text-slate-500 mt-1">
              Find this in your {pspType === 'stripe' ? 'Stripe' : 'Adyen'} dashboard
            </p>
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
              Connect Payment Provider
            </>
          )}
        </button>

        <p className="text-xs text-center text-slate-500">
          Your API keys are encrypted and stored securely
        </p>
      </form>
    </div>
  );
}

