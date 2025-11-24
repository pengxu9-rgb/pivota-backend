import { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { CheckCircle, Store, Package, ShoppingCart, CreditCard, ArrowRight, AlertCircle } from 'lucide-react';

interface CompletionStepProps {
  data: {
    merchant_id?: string;
    business_name?: string;
    auto_approved?: boolean;
    message?: string;
  };
}

export default function CompletionStep({ data }: CompletionStepProps) {
  const router = useRouter();
  const [checklist, setChecklist] = useState({
    storeConnected: false,
    productsLoaded: false,
    pspConfigured: true, // Already done in previous step
    testOrderCreated: false
  });

  const checklistItems = [
    {
      id: 'pspConfigured',
      icon: CreditCard,
      title: 'Payment Provider Connected',
      description: 'You can now accept payments',
      status: 'completed',
      action: null
    },
    {
      id: 'storeConnected',
      icon: Store,
      title: 'Connect Your Store',
      description: 'Link Shopify, Wix, or custom platform',
      status: checklist.storeConnected ? 'completed' : 'pending',
      action: {
        label: 'Connect Store',
        path: '/dashboard/integrations'
      }
    },
    {
      id: 'productsLoaded',
      icon: Package,
      title: 'Sync Your Products',
      description: 'Import products from your store or add manually',
      status: checklist.productsLoaded ? 'completed' : 'pending',
      action: {
        label: 'Manage Products',
        path: '/dashboard/products'
      }
    },
    {
      id: 'testOrderCreated',
      icon: ShoppingCart,
      title: 'Test Your Payment Flow',
      description: 'Create a test order to verify everything works',
      status: checklist.testOrderCreated ? 'completed' : 'pending',
      action: {
        label: 'Test Order',
        path: '/dashboard/orders'
      }
    }
  ];

  const handleGoToDashboard = () => {
    router.push('/login');
  };

  return (
    <div className="max-w-3xl mx-auto">
      {/* Success Header */}
      <div className="text-center mb-8">
        <div className="inline-flex items-center justify-center w-20 h-20 bg-green-100 rounded-full mb-4">
          <CheckCircle className="w-12 h-12 text-green-600" />
        </div>
        <h2 className="text-3xl font-bold text-slate-900 mb-2">
          Registration Complete! 🎉
        </h2>
        <p className="text-lg text-slate-600">
          Welcome to Pivota, {data.business_name}!
        </p>
      </div>

      {/* Account Info */}
      <div className="bg-blue-50 border border-blue-200 rounded-lg p-6 mb-8">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <p className="text-sm text-blue-700 mb-1">Merchant ID</p>
            <p className="font-mono text-sm font-semibold text-blue-900">{data.merchant_id}</p>
          </div>
          <div>
            <p className="text-sm text-blue-700 mb-1">Status</p>
            <div className="inline-flex items-center gap-2 px-3 py-1 bg-green-100 rounded-full">
              <CheckCircle className="w-4 h-4 text-green-700" />
              <span className="text-sm font-medium text-green-700">
                {data.auto_approved ? 'Auto-Approved' : 'Pending Review'}
              </span>
            </div>
          </div>
        </div>
      </div>

      {/* Next Steps Checklist */}
      <div className="bg-white border border-slate-200 rounded-lg p-6 mb-8">
        <h3 className="text-xl font-bold text-slate-900 mb-4">
          🚀 Next Steps: Get Ready for Transactions
        </h3>
        <p className="text-sm text-slate-600 mb-6">
          Complete these steps to start processing payments and managing orders
        </p>

        <div className="space-y-4">
          {checklistItems.map((item) => {
            const Icon = item.icon;
            return (
              <div
                key={item.id}
                className={`flex items-start gap-4 p-4 rounded-lg border-2 transition-all ${
                  item.status === 'completed'
                    ? 'border-green-200 bg-green-50'
                    : 'border-slate-200 bg-white hover:border-blue-300'
                }`}
              >
                <div
                  className={`w-10 h-10 rounded-full flex items-center justify-center flex-shrink-0 ${
                    item.status === 'completed'
                      ? 'bg-green-600'
                      : 'bg-slate-200'
                  }`}
                >
                  {item.status === 'completed' ? (
                    <CheckCircle className="w-6 h-6 text-white" />
                  ) : (
                    <Icon className="w-6 h-6 text-slate-600" />
                  )}
                </div>
                
                <div className="flex-1">
                  <h4 className="font-semibold text-slate-900">{item.title}</h4>
                  <p className="text-sm text-slate-600 mt-1">{item.description}</p>
                  {item.status === 'completed' && (
                    <p className="text-xs text-green-600 mt-2 font-medium">✓ Completed</p>
                  )}
                </div>

                {item.status === 'pending' && item.action && (
                  <button
                    onClick={handleGoToDashboard}
                    className="flex-shrink-0 text-sm text-blue-600 font-medium hover:text-blue-700 hover:underline"
                  >
                    → Do this in Dashboard
                  </button>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* Important Notice */}
      {data.auto_approved && (
        <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-4 mb-8">
          <div className="flex items-start gap-3">
            <AlertCircle className="w-5 h-5 text-yellow-600 mt-0.5 flex-shrink-0" />
            <div className="text-sm text-yellow-800">
              <p className="font-medium mb-1">⚠️ KYB Documentation Required</p>
              <p>
                Your account is pre-approved to start testing immediately. However, you must complete
                full KYB documentation within <strong>7 days</strong> to continue processing live transactions.
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Action Buttons */}
      <div className="space-y-3">
        <button
          onClick={handleGoToDashboard}
          className="w-full px-6 py-4 bg-gradient-to-r from-blue-500 to-indigo-600 text-white font-semibold text-lg rounded-lg hover:from-blue-600 hover:to-indigo-700 flex items-center justify-center gap-3 shadow-lg"
        >
          Go to Dashboard
          <ArrowRight className="w-6 h-6" />
        </button>

        {/* Documentation link - coming soon
        <a
          href="https://docs.pivota.com/merchant-quickstart"
          target="_blank"
          rel="noopener noreferrer"
          className="block w-full px-6 py-3 text-center border-2 border-blue-600 text-blue-600 font-semibold rounded-lg hover:bg-blue-50 transition-colors"
        >
          📚 View Quickstart Guide
        </a>
        */}
      </div>

      {/* Tips */}
      <div className="mt-8 p-4 bg-purple-50 border border-purple-200 rounded-lg">
        <h4 className="font-semibold text-purple-900 mb-2">💡 Pro Tips</h4>
        <ul className="text-sm text-purple-800 space-y-1">
          <li>• Connect your store to automatically sync products and inventory</li>
          <li>• Create a test order to verify your payment flow end-to-end</li>
          <li>• Set up webhooks to get real-time order notifications</li>
          <li>• Invite AI agents to help customers shop on your store</li>
        </ul>
      </div>

      {/* Support */}
      <div className="mt-6 text-center">
        <p className="text-sm text-slate-600">
          Need help getting started?{' '}
          <a href="mailto:support@pivota.cc" className="text-blue-600 hover:underline font-medium">
            Contact Support
          </a>
        </p>
      </div>
    </div>
  );
}
