import { useState } from 'react';
import { Store, Loader, AlertCircle, CheckCircle, ArrowRight, Code } from 'lucide-react';

interface StoreConnectionStepProps {
  merchantId: string;
  storeUrl: string; // From registration step
  onComplete: () => void;
  onSkip: () => void;
}

type Platform = 'shopify' | 'wix' | 'woocommerce' | 'custom' | null;

export default function StoreConnectionStep({ merchantId, storeUrl, onComplete, onSkip }: StoreConnectionStepProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [selectedPlatform, setSelectedPlatform] = useState<Platform>(null);

  // Auto-detect platform from store URL
  const detectPlatform = (): Platform => {
    const url = storeUrl.toLowerCase();
    if (url.includes('myshopify.com')) return 'shopify';
    if (url.includes('wix.com') || url.includes('wixsite.com')) return 'wix';
    if (url.includes('wordpress') || url.includes('woocommerce')) return 'woocommerce';
    return 'custom';
  };

  const detectedPlatform = detectPlatform();

  const platforms = [
    {
      id: 'shopify',
      name: 'Shopify',
      icon: '🛍️',
      description: 'OAuth integration (recommended)',
      supported: true,
      autoDetected: detectedPlatform === 'shopify'
    },
    {
      id: 'wix',
      name: 'Wix',
      icon: '🌐',
      description: 'API Key integration',
      supported: true,
      autoDetected: detectedPlatform === 'wix'
    },
    {
      id: 'woocommerce',
      name: 'WooCommerce',
      icon: '🔌',
      description: 'Coming soon - use API for now',
      supported: false,
      autoDetected: detectedPlatform === 'woocommerce'
    },
    {
      id: 'custom',
      name: 'Custom Website',
      icon: '⚙️',
      description: 'Use Pivota API directly',
      supported: true,
      autoDetected: detectedPlatform === 'custom'
    }
  ];

  const handleConnect = async () => {
    const platform = selectedPlatform || detectedPlatform;
    
    if (platform === 'shopify') {
      setLoading(true);
      try {
        const shopDomain = storeUrl.replace('https://', '').replace('http://', '').split('/')[0];
        const response = await fetch(
          `https://web-production-fedb.up.railway.app/integrations/shopify/oauth/start?merchant_id=${merchantId}&shop=${shopDomain}`
        );
        const data = await response.json();
        
        if (data.authorize) {
          window.location.href = data.authorize;
        } else {
          throw new Error('Failed to start OAuth');
        }
      } catch (err: any) {
        setError(err.message || 'Failed to connect Shopify');
        setLoading(false);
      }
    } else if (platform === 'wix') {
      // Wix needs manual credentials
      alert('Wix integration requires API credentials. You can set this up later in Dashboard > Integrations.');
      onSkip();
    } else if (platform === 'custom') {
      // Custom sites use Pivota API directly
      onSkip();
    } else {
      // Platform not supported yet
      onSkip();
    }
  };

  return (
    <div>
      <div className="mb-6">
        <h2 className="text-2xl font-bold text-slate-900">Connect Your Store</h2>
        <p className="text-sm text-slate-600 mt-1">
          Link your store to automatically sync products and inventory
        </p>
      </div>

      {/* Detected Store */}
      <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 mb-6">
        <div className="flex items-start gap-3">
          <Store className="w-5 h-5 text-blue-600 mt-0.5" />
          <div className="flex-1">
            <p className="font-medium text-blue-900">Your Store URL</p>
            <p className="text-sm text-blue-800 mt-1 break-all">{storeUrl}</p>
            {detectedPlatform !== 'custom' && (
              <div className="mt-2 inline-flex items-center gap-2 px-3 py-1 bg-blue-100 rounded-full">
                <CheckCircle className="w-4 h-4 text-blue-700" />
                <span className="text-xs font-medium text-blue-700">
                  {detectedPlatform === 'shopify' && 'Shopify store detected'}
                  {detectedPlatform === 'wix' && 'Wix store detected'}
                  {detectedPlatform === 'woocommerce' && 'WooCommerce store detected'}
                </span>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Platform Selection */}
      <div className="space-y-3 mb-6">
        <label className="block text-sm font-medium text-slate-700 mb-2">
          Select Platform Type
        </label>
        {platforms.map((p) => (
          <button
            key={p.id}
            type="button"
            onClick={() => setSelectedPlatform(p.id as Platform)}
            disabled={!p.supported}
            className={`w-full p-4 border-2 rounded-lg text-left transition-all ${
              selectedPlatform === p.id || (!selectedPlatform && p.autoDetected)
                ? 'border-blue-500 bg-blue-50 ring-2 ring-blue-200'
                : p.supported
                ? 'border-slate-200 hover:border-slate-300 hover:bg-slate-50'
                : 'border-slate-200 bg-slate-50 opacity-60 cursor-not-allowed'
            }`}
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className="text-3xl">{p.icon}</div>
                <div>
                  <div className="font-semibold text-slate-900 flex items-center gap-2">
                    {p.name}
                    {p.autoDetected && (
                      <span className="text-xs px-2 py-0.5 bg-blue-100 text-blue-700 rounded-full">
                        Auto-detected
                      </span>
                    )}
                    {!p.supported && (
                      <span className="text-xs px-2 py-0.5 bg-gray-100 text-gray-600 rounded-full">
                        Coming Soon
                      </span>
                    )}
                  </div>
                  <div className="text-sm text-slate-600">{p.description}</div>
                </div>
              </div>
              {p.supported && (selectedPlatform === p.id || (!selectedPlatform && p.autoDetected)) && (
                <CheckCircle className="w-5 h-5 text-blue-600" />
              )}
            </div>
          </button>
        ))}
      </div>

      {/* Info for Custom Platform */}
      {(selectedPlatform === 'custom' || (!selectedPlatform && detectedPlatform === 'custom')) && (
        <div className="bg-purple-50 border border-purple-200 rounded-lg p-4 mb-6">
          <div className="flex items-start gap-3">
            <Code className="w-5 h-5 text-purple-600 mt-0.5" />
            <div className="text-sm text-purple-800">
              <p className="font-medium mb-1">Custom Website Integration</p>
              <p>
                For custom websites, you'll use Pivota's API directly to create orders and process payments.
                Your API key will be provided in the next step.
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Error Message */}
      {error && (
        <div className="flex items-center gap-2 p-4 bg-red-50 border border-red-200 rounded-lg mb-6">
          <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0" />
          <p className="text-sm text-red-700">{error}</p>
        </div>
      )}

      {/* Actions */}
      <div className="space-y-3">
        {((selectedPlatform || detectedPlatform) === 'shopify' || (selectedPlatform || detectedPlatform) === 'wix') ? (
          <button
            onClick={handleConnect}
            disabled={loading}
            className="w-full px-4 py-3 bg-gradient-to-r from-blue-500 to-indigo-600 text-white font-medium rounded-lg hover:from-blue-600 hover:to-indigo-700 disabled:opacity-50 flex items-center justify-center gap-2"
          >
            {loading ? (
              <>
                <Loader className="w-5 h-5 animate-spin" />
                Connecting...
              </>
            ) : (
              <>
                <CheckCircle className="w-5 h-5" />
                Connect {selectedPlatform === 'shopify' ? 'Shopify' : 'Wix'} Store
              </>
            )}
          </button>
        ) : (
          <button
            onClick={onSkip}
            className="w-full px-4 py-3 bg-gradient-to-r from-blue-500 to-indigo-600 text-white font-medium rounded-lg hover:from-blue-600 hover:to-indigo-700 flex items-center justify-center gap-2"
          >
            Continue
            <ArrowRight className="w-5 h-5" />
          </button>
        )}

        <button
          onClick={onSkip}
          className="w-full px-4 py-3 border-2 border-slate-300 text-slate-700 font-medium rounded-lg hover:bg-slate-50"
        >
          I'll Connect Later
        </button>
      </div>

      <p className="text-xs text-center text-slate-500 mt-4">
        You can always connect or change your store in Dashboard &gt; Integrations
      </p>
    </div>
  );
}
