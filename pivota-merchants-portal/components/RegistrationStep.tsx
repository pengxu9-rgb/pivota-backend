import { useState } from 'react';
import { Loader, CheckCircle, AlertCircle } from 'lucide-react';
import { onboardingApi } from '../lib/api';

interface RegistrationStepProps {
  onComplete: (data: any) => void;
}

export default function RegistrationStep({ onComplete }: RegistrationStepProps) {
  const [formData, setFormData] = useState({
    business_name: '',
    store_url: 'https://',
    region: '',
    contact_email: '',
    contact_phone: '',
    password: '',
    confirm_password: '',
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError('');

    // Validate passwords match
    if (formData.password !== formData.confirm_password) {
      setError('Passwords do not match');
      setLoading(false);
      return;
    }

    // Prepare data without confirm_password field
    const { confirm_password, ...registrationData } = formData;

    try {
      const response = await onboardingApi.register(registrationData);
      
      // Pass data to parent
      onComplete({
        merchant_id: response.merchant_id,
        business_name: formData.business_name,
        auto_approved: response.auto_approved,
        confidence_score: response.confidence_score,
        message: response.message,
      });
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Registration failed. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <div className="mb-6">
        <h2 className="text-2xl font-bold text-slate-900">Business Information</h2>
        <p className="text-sm text-slate-600 mt-1">
          Tell us about your business (takes ~30 seconds)
        </p>
      </div>

      <form onSubmit={handleSubmit} className="space-y-4">
        {/* Business Name */}
        <div>
          <label className="block text-sm font-medium text-slate-700 mb-1">
            Business Name <span className="text-red-500">*</span>
          </label>
          <input
            type="text"
            required
            value={formData.business_name}
            onChange={(e) => setFormData({ ...formData, business_name: e.target.value })}
            className="w-full px-4 py-2 border border-slate-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            placeholder="e.g., My Coffee Shop"
          />
        </div>

        {/* Store URL */}
        <div>
          <label className="block text-sm font-medium text-slate-700 mb-1">
            Store URL <span className="text-red-500">*</span>
          </label>
          <input
            type="url"
            required
            value={formData.store_url}
            onChange={(e) => {
              let value = e.target.value;
              // Ensure https:// prefix is maintained
              if (!value.startsWith('https://') && !value.startsWith('http://')) {
                value = 'https://' + value.replace(/^[htps:/]*/, '');
              }
              setFormData({ ...formData, store_url: value });
            }}
            onFocus={(e) => {
              // If empty or just https://, position cursor after https://
              if (e.target.value === 'https://') {
                setTimeout(() => e.target.setSelectionRange(8, 8), 0);
              }
            }}
            className="w-full px-4 py-2 border border-slate-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            placeholder="https://mystore.myshopify.com"
          />
          <p className="text-xs text-slate-500 mt-1">
            Your Shopify, Wix, WooCommerce, or custom store URL
          </p>
        </div>

        {/* Region */}
        <div>
          <label className="block text-sm font-medium text-slate-700 mb-1">
            Region <span className="text-red-500">*</span>
          </label>
          <select
            required
            value={formData.region}
            onChange={(e) => setFormData({ ...formData, region: e.target.value })}
            className="w-full px-4 py-2 border border-slate-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
          >
            <option value="">Select region...</option>
            <option value="US">United States</option>
            <option value="CA">Canada</option>
            <option value="UK">United Kingdom</option>
            <option value="EU">European Union</option>
            <option value="APAC">Asia Pacific</option>
            <option value="Other">Other</option>
          </select>
        </div>

        {/* Contact Email */}
        <div>
          <label className="block text-sm font-medium text-slate-700 mb-1">
            Contact Email <span className="text-red-500">*</span>
          </label>
          <input
            type="email"
            required
            value={formData.contact_email}
            onChange={(e) => setFormData({ ...formData, contact_email: e.target.value })}
            className="w-full px-4 py-2 border border-slate-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            placeholder="your@email.com"
          />
        </div>

        {/* Contact Phone */}
        <div>
          <label className="block text-sm font-medium text-slate-700 mb-1">
            Contact Phone
          </label>
          <input
            type="tel"
            value={formData.contact_phone}
            onChange={(e) => setFormData({ ...formData, contact_phone: e.target.value })}
            className="w-full px-4 py-2 border border-slate-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            placeholder="+1 (555) 123-4567"
          />
        </div>

        {/* Password */}
        <div>
          <label className="block text-sm font-medium text-slate-700 mb-1">
            Password <span className="text-red-500">*</span>
          </label>
          <input
            type="password"
            required
            value={formData.password}
            onChange={(e) => setFormData({ ...formData, password: e.target.value })}
            className="w-full px-4 py-2 border border-slate-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            placeholder="Enter a secure password"
            minLength={8}
          />
          <p className="text-xs text-slate-500 mt-1">
            At least 8 characters long
          </p>
        </div>

        {/* Confirm Password */}
        <div>
          <label className="block text-sm font-medium text-slate-700 mb-1">
            Confirm Password <span className="text-red-500">*</span>
          </label>
          <input
            type="password"
            required
            value={formData.confirm_password}
            onChange={(e) => setFormData({ ...formData, confirm_password: e.target.value })}
            className="w-full px-4 py-2 border border-slate-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            placeholder="Re-enter your password"
          />
          {formData.password && formData.confirm_password && formData.password !== formData.confirm_password && (
            <p className="text-xs text-red-600 mt-1">Passwords do not match</p>
          )}
        </div>

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
          disabled={loading}
          className="w-full py-3 px-4 bg-gradient-to-r from-blue-500 to-indigo-600 text-white font-medium rounded-lg hover:from-blue-600 hover:to-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
        >
          {loading ? (
            <>
              <Loader className="w-5 h-5 animate-spin" />
              Registering...
            </>
          ) : (
            <>
              <CheckCircle className="w-5 h-5" />
              Register Business
            </>
          )}
        </button>
      </form>
    </div>
  );
}


