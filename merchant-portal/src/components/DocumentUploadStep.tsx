import { useState } from 'react';
import { Loader, CheckCircle, AlertCircle, Upload, File, X } from 'lucide-react';
import { onboardingApi, integrationsApi } from '../lib/api';

interface DocumentUploadStepProps {
  merchantId: string;
  onComplete: () => void;
  autoApproved?: boolean;
}

export default function DocumentUploadStep({ merchantId, onComplete, autoApproved }: DocumentUploadStepProps) {
  const [files, setFiles] = useState<File[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) {
      setFiles([...files, ...Array.from(e.target.files)]);
    }
  };

  const removeFile = (index: number) => {
    setFiles(files.filter((_, i) => i !== index));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    
    if (files.length === 0) {
      // If auto-approved, allow skipping
      if (autoApproved) {
        onComplete();
        return;
      } else {
        setError('Please upload at least one document');
        return;
      }
    }

    setLoading(true);
    setError('');

    try {
      await onboardingApi.uploadDocuments(merchantId, files);
      onComplete();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Upload failed. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  const handleSkip = () => {
    if (autoApproved) {
      onComplete();
    }
  };

  return (
    <div>
      <div className="mb-6">
        <h2 className="text-2xl font-bold text-slate-900">Upload Documents</h2>
        <p className="text-sm text-slate-600 mt-1">
          {autoApproved ? (
            <span className="text-green-600 font-medium">
              ✓ Pre-approved! You have 7 days to complete full KYB documentation
            </span>
          ) : (
            'Upload your business verification documents'
          )}
        </p>
      </div>

      {/* Connect Shopify (OAuth) */}
      <div className="mb-6 p-4 border border-emerald-200 bg-emerald-50 rounded-lg">
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
          <div>
            <div className="font-medium text-emerald-900">Connect your store (Shopify)</div>
            <div className="text-sm text-emerald-700">Authorize access to sync products and orders</div>
          </div>
          <div className="flex items-center gap-2">
            <input
              type="text"
              placeholder="yourshop.myshopify.com"
              className="px-3 py-2 text-sm border border-emerald-300 rounded-md focus:outline-none focus:ring-2 focus:ring-emerald-500"
              onChange={(e) => (shopDomainRef = e.target.value)}
            />
            <button
              type="button"
              onClick={async () => {
                try {
                  const res = await integrationsApi.startShopifyOAuth(merchantId, shopDomainRef || '');
                  if (res.authorize) {
                    window.location.href = res.authorize;
                  }
                } catch (e: any) {
                  alert('Start OAuth failed: ' + (e.response?.data?.detail || e.message));
                }
              }}
              className="px-4 py-2 text-sm font-medium text-white bg-emerald-600 rounded-md hover:bg-emerald-700"
            >
              Connect Shopify
            </button>
          </div>
        </div>
      </div>

      <form onSubmit={handleSubmit} className="space-y-6">
        {/* Upload Area */}
        <div className="border-2 border-dashed border-slate-300 rounded-lg p-8 text-center hover:border-blue-400 transition-colors">
          <input
            type="file"
            id="file-upload"
            multiple
            accept=".pdf,.jpg,.jpeg,.png"
            onChange={handleFileChange}
            className="hidden"
          />
          <label htmlFor="file-upload" className="cursor-pointer">
            <Upload className="w-12 h-12 mx-auto text-slate-400 mb-4" />
            <p className="text-sm font-medium text-slate-700 mb-1">
              Click to upload or drag and drop
            </p>
            <p className="text-xs text-slate-500">
              PDF, JPG, PNG (max 10MB each)
            </p>
          </label>
        </div>

        {/* File List */}
        {files.length > 0 && (
          <div className="space-y-2">
            <p className="text-sm font-medium text-slate-700">Selected Files ({files.length})</p>
            {files.map((file, index) => (
              <div
                key={index}
                className="flex items-center justify-between p-3 bg-slate-50 border border-slate-200 rounded-lg"
              >
                <div className="flex items-center gap-3">
                  <File className="w-5 h-5 text-slate-400" />
                  <div>
                    <p className="text-sm font-medium text-slate-700">{file.name}</p>
                    <p className="text-xs text-slate-500">
                      {(file.size / 1024 / 1024).toFixed(2)} MB
                    </p>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => removeFile(index)}
                  className="p-1 hover:bg-slate-200 rounded"
                >
                  <X className="w-4 h-4 text-slate-500" />
                </button>
              </div>
            ))}
          </div>
        )}

        {/* Required Documents Info */}
        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
          <p className="text-sm font-medium text-blue-900 mb-2">Recommended Documents:</p>
          <ul className="text-sm text-blue-700 space-y-1">
            <li>• Business registration certificate</li>
            <li>• Tax identification document</li>
            <li>• Bank account statement</li>
            <li>• Owner ID (passport or driver's license)</li>
          </ul>
        </div>

        {/* Error Message */}
        {error && (
          <div className="flex items-center gap-2 p-4 bg-red-50 border border-red-200 rounded-lg">
            <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0" />
            <p className="text-sm text-red-700">{error}</p>
          </div>
        )}

        {/* Submit Buttons */}
        <div className="flex gap-3">
          {autoApproved && (
            <button
              type="button"
              onClick={handleSkip}
              className="flex-1 py-3 px-4 border border-slate-300 text-slate-700 font-medium rounded-lg hover:bg-slate-50"
            >
              Skip for Now
            </button>
          )}
          <button
            type="submit"
            disabled={loading}
            className="flex-1 py-3 px-4 bg-gradient-to-r from-blue-500 to-indigo-600 text-white font-medium rounded-lg hover:from-blue-600 hover:to-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
          >
            {loading ? (
              <>
                <Loader className="w-5 h-5 animate-spin" />
                Uploading...
              </>
            ) : (
              <>
                <CheckCircle className="w-5 h-5" />
                {files.length > 0 ? 'Upload Documents' : 'Continue'}
              </>
            )}
          </button>
        </div>
      </form>
    </div>
  );
}

