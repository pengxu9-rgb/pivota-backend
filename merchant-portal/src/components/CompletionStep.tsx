import { CheckCircle, Rocket, Clock, AlertCircle } from 'lucide-react';

interface CompletionStepProps {
  data: {
    merchant_id?: string;
    business_name?: string;
    auto_approved?: boolean;
    confidence_score?: number;
    message?: string;
  };
}

export default function CompletionStep({ data }: CompletionStepProps) {
  return (
    <div className="text-center py-8">
      {/* Success Icon */}
      <div className="inline-flex items-center justify-center w-20 h-20 bg-green-100 rounded-full mb-6">
        <Rocket className="w-10 h-10 text-green-600" />
      </div>

      {/* Title */}
      <h2 className="text-3xl font-bold text-slate-900 mb-2">
        {data.auto_approved ? '🎉 Congratulations!' : 'Application Submitted!'}
      </h2>

      {/* Subtitle */}
      <p className="text-lg text-slate-600 mb-8">
        {data.auto_approved
          ? 'Your business has been automatically approved!'
          : 'Your onboarding application is under review'}
      </p>

      {/* Status Card */}
      <div className="max-w-md mx-auto mb-8">
        <div
          className={`p-6 rounded-xl border-2 ${
            data.auto_approved
              ? 'bg-green-50 border-green-200'
              : 'bg-blue-50 border-blue-200'
          }`}
        >
          <div className="flex items-start gap-4">
            {data.auto_approved ? (
              <CheckCircle className="w-8 h-8 text-green-600 flex-shrink-0 mt-1" />
            ) : (
              <Clock className="w-8 h-8 text-blue-600 flex-shrink-0 mt-1" />
            )}
            <div className="text-left">
              <h3 className="font-semibold text-lg mb-1">
                {data.auto_approved ? 'Auto-Approved' : 'Under Review'}
              </h3>
              <p className="text-sm text-slate-700">{data.message}</p>
              {data.confidence_score && (
                <p className="text-xs text-slate-500 mt-2">
                  Confidence Score: {(data.confidence_score * 100).toFixed(0)}%
                </p>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Next Steps */}
      <div className="max-w-md mx-auto text-left mb-8">
        <h3 className="font-semibold text-slate-900 mb-3">Next Steps:</h3>
        <div className="space-y-3">
          {data.auto_approved ? (
            <>
              <div className="flex items-start gap-3">
                <CheckCircle className="w-5 h-5 text-green-600 flex-shrink-0 mt-0.5" />
                <p className="text-sm text-slate-700">
                  Start accepting payments immediately
                </p>
              </div>
              <div className="flex items-start gap-3">
                <AlertCircle className="w-5 h-5 text-amber-600 flex-shrink-0 mt-0.5" />
                <p className="text-sm text-slate-700">
                  Complete full KYB documentation within 7 days
                </p>
              </div>
              <div className="flex items-start gap-3">
                <CheckCircle className="w-5 h-5 text-green-600 flex-shrink-0 mt-0.5" />
                <p className="text-sm text-slate-700">
                  Connect your store via MCP for product sync
                </p>
              </div>
            </>
          ) : (
            <>
              <div className="flex items-start gap-3">
                <Clock className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />
                <p className="text-sm text-slate-700">
                  We'll review your application within 1-2 business days
                </p>
              </div>
              <div className="flex items-start gap-3">
                <CheckCircle className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />
                <p className="text-sm text-slate-700">
                  You'll receive an email once approved
                </p>
              </div>
              <div className="flex items-start gap-3">
                <CheckCircle className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />
                <p className="text-sm text-slate-700">
                  Upload additional documents if requested
                </p>
              </div>
            </>
          )}
        </div>
      </div>

      {/* Merchant ID */}
      <div className="bg-slate-50 border border-slate-200 rounded-lg p-4 max-w-md mx-auto mb-6">
        <p className="text-xs text-slate-500 mb-1">Your Merchant ID</p>
        <p className="font-mono text-sm font-medium text-slate-900">{data.merchant_id}</p>
        <p className="text-xs text-slate-500 mt-1">Save this for future reference</p>
      </div>

      {/* Action Button */}
      <button
        onClick={() => window.location.reload()}
        className="px-6 py-3 bg-gradient-to-r from-blue-500 to-indigo-600 text-white font-medium rounded-lg hover:from-blue-600 hover:to-indigo-700"
      >
        Register Another Business
      </button>

      {/* Support */}
      <p className="text-sm text-slate-500 mt-8">
        Need help? Contact us at{' '}
        <a href="mailto:support@pivota.com" className="text-blue-600 hover:underline">
          support@pivota.com
        </a>
      </p>
    </div>
  );
}

