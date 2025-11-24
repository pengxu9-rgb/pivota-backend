'use client';

import { FormEvent, useEffect, useState } from 'react';
import { Store, Database, FileText, Rocket } from 'lucide-react';
import { FEATURE_FLAGS } from '@/lib/config';
import { platformOnboardingApi } from '@/lib/api';

type StepId = 'intro' | 'datasource' | 'reports' | 'coming-soon';

const STEPS: { id: StepId; title: string; icon: any; description: string }[] = [
  {
    id: 'intro',
    title: 'Overview',
    icon: Store,
    description: 'Understand how platform merchants connect to Pivota.',
  },
  {
    id: 'datasource',
    title: 'Data source',
    icon: Database,
    description: 'Choose between multi-channel tools and platform reports.',
  },
  {
    id: 'reports',
    title: 'Platform reports',
    icon: FileText,
    description: 'Upload Amazon/Temu CSV and preview orderable products.',
  },
  {
    id: 'coming-soon',
    title: 'Import & launch',
    icon: Rocket,
    description: 'Catalog import, MDQS and agent visibility (coming next).',
  },
];

interface ValidatePreviewRow {
  row_number: number;
  row_data: Record<string, string>;
  missing_required_fields: string[];
  valid: boolean;
}

interface ValidateResponse {
  report_type: string;
  required_columns: string[];
  found_columns: string[];
  missing_columns: string[];
  rows_scanned: number;
  rows_with_missing_required_fields: number;
  ready_to_import: boolean;
  preview: ValidatePreviewRow[];
}

interface StandardProduct {
  id: string;
  platform: string;
  merchant_id: string;
  title: string;
  price: number;
  currency: string;
  orderable?: boolean;
  orderable_validation?: {
    orderable?: boolean;
    errors?: string[];
  } | null;
}

export default function PlatformOnboardingPage() {
  const [currentStep, setCurrentStep] = useState<StepId>('intro');
  const [featureEnabled, setFeatureEnabled] = useState<boolean>(FEATURE_FLAGS.PLATFORM_ONBOARDING_V2);
  const [probing, setProbing] = useState<boolean>(false);
  const [formData, setFormData] = useState({
    business_name: '',
    region: 'US',
    source_type: 'connector',
    contact_email: '',
    store_url: '',
  });
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitResult, setSubmitResult] = useState<any | null>(null);
  const [importTasks, setImportTasks] = useState<any[] | null>(null);
  const [reportType, setReportType] = useState<'amazon' | 'temu'>('amazon');
  const [file, setFile] = useState<File | null>(null);
  const [validating, setValidating] = useState(false);
  const [validateResult, setValidateResult] = useState<ValidateResponse | null>(null);
  const [uploading, setUploading] = useState(false);
  const [productsLoading, setProductsLoading] = useState(false);
  const [products, setProducts] = useState<StandardProduct[]>([]);
  const [orderProductId, setOrderProductId] = useState('');
  const [orderVariantId, setOrderVariantId] = useState('');
  const [orderQuantity, setOrderQuantity] = useState(1);
  const [ordering, setOrdering] = useState(false);
  const [orderResult, setOrderResult] = useState<any | null>(null);

  useEffect(() => {
    if (!FEATURE_FLAGS.PLATFORM_ONBOARDING_V2) return;
    const probe = async () => {
      try {
        setProbing(true);
        const res = await platformOnboardingApi.getFeatureStatus();
        if (res && res.enabled) {
          setFeatureEnabled(true);
        }
      } catch (e) {
        // If backend flag is off we keep the page but show a disabled state.
        setFeatureEnabled(false);
      } finally {
        setProbing(false);
      }
    };
    probe();
  }, []);

  const handleRegister = async (e: FormEvent) => {
    e.preventDefault();
    if (!featureEnabled) return;

    setSubmitting(true);
    setSubmitError(null);
    setSubmitResult(null);
    setImportTasks(null);

    try {
      const payload: any = {
        business_name: formData.business_name,
        region: formData.region,
        source_type: formData.source_type,
      };
      if (formData.contact_email) {
        payload.contact_email = formData.contact_email;
      }
      if (formData.store_url) {
        payload.store_url = formData.store_url;
      }

      const created = await platformOnboardingApi.register(payload);
      let tasks: any[] | null = null;

      // Directly use register response - it always has correct platform_profile
      // Skip getDetails to avoid data inconsistency issues
      console.log('[Platform Onboarding] Using register response directly');
      
      if (created && created.onboarding_id) {
        try {
          const tRes = await platformOnboardingApi.listImportTasks(created.onboarding_id);
          tasks = Array.isArray(tRes.tasks) ? tRes.tasks : null;
        } catch (err) {
          console.error('Platform onboarding listImportTasks failed', err);
        }
      }

      setSubmitResult(created);
      if (tasks) {
        setImportTasks(tasks);
      }
    } catch (err: any) {
      console.error('Platform onboarding register failed', err);
      const detail =
        err?.response?.data?.detail ||
        err?.message ||
        'Registration failed. Please try again.';
      setSubmitError(detail);
    } finally {
      setSubmitting(false);
    }
  };

  const handleRefreshTasks = async () => {
    if (!submitResult?.onboarding_id) return;
    try {
      const tRes = await platformOnboardingApi.listImportTasks(submitResult.onboarding_id);
      const tasks = Array.isArray(tRes.tasks) ? tRes.tasks : null;
      setImportTasks(tasks);
    } catch (err) {
      console.error('Platform onboarding refresh import tasks failed', err);
    }
  };

  const onboardingId = submitResult?.onboarding_id as string | undefined;

  const handleValidateReport = async () => {
    if (!onboardingId || !file) return;
    setValidating(true);
    setValidateResult(null);
    try {
      const data = await platformOnboardingApi.validateReport(onboardingId, reportType, file);
      setValidateResult(data as ValidateResponse);
    } catch (err: any) {
      console.error('Platform report validate failed', err);
      const detail =
        err?.response?.data?.detail ||
        err?.message ||
        'Validation failed. Please check your CSV file.';
      setValidateResult(null);
      alert(detail);
    } finally {
      setValidating(false);
    }
  };

  const handleUploadReport = async () => {
    if (!onboardingId || !file || !validateResult?.ready_to_import) return;
    setUploading(true);
    try {
      const data = await platformOnboardingApi.uploadReport(onboardingId, reportType, file);
      alert('Report uploaded. Import task started.');
      if (data?.import_task_id) {
        const tRes = await platformOnboardingApi.listImportTasks(onboardingId);
        const tasks = Array.isArray(tRes.tasks) ? tRes.tasks : null;
        setImportTasks(tasks);
      }
    } catch (err: any) {
      console.error('Platform report upload failed', err);
      const detail =
        err?.response?.data?.detail ||
        err?.message ||
        'Upload failed. Please try again.';
      alert(detail);
    } finally {
      setUploading(false);
    }
  };

  const handleRefreshProducts = async () => {
    if (!onboardingId) return;
    setProductsLoading(true);
    setProducts([]);
    try {
      const data = await platformOnboardingApi.getOrderableProducts(
        onboardingId,
        reportType,
        20
      );
      const list = (data?.products || []) as StandardProduct[];
      setProducts(list);
    } catch (err: any) {
      console.error('Fetch orderable products failed', err);
      const detail =
        err?.response?.data?.detail ||
        err?.message ||
        'Failed to load products.';
      alert(detail);
    } finally {
      setProductsLoading(false);
    }
  };

  const handleCreateStubOrder = async () => {
    if (!onboardingId || !orderProductId) return;
    setOrdering(true);
    setOrderResult(null);
    try {
      const payload = {
        platform: reportType,
        platform_product_id: orderProductId.trim(),
        variant_id: orderVariantId.trim() || undefined,
        quantity: orderQuantity > 0 ? orderQuantity : 1,
      } as const;
      const data = await platformOnboardingApi.createPlatformOrderPoc(onboardingId, payload);
      setOrderResult(data);
    } catch (err: any) {
      console.error('Create stub order failed', err);
      const detail =
        err?.response?.data?.detail ||
        err?.message ||
        'Failed to create test order.';
      alert(detail);
    } finally {
      setOrdering(false);
    }
  };

  if (!FEATURE_FLAGS.PLATFORM_ONBOARDING_V2) {
    return (
      <div className="max-w-3xl mx-auto space-y-4">
        <h1 className="text-2xl font-bold text-gray-900">Platform Merchant Onboarding</h1>
        <p className="text-gray-600 text-sm">
          This feature is currently disabled for this environment. Set
          <code className="mx-1 bg-gray-100 px-1 rounded">NEXT_PUBLIC_FEATURE_PLATFORM_ONBOARDING_V2=true</code>
          and redeploy the merchant portal to enable it.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-8 max-w-5xl mx-auto">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Platform Merchant Onboarding (v2)</h1>
          <p className="text-gray-600 text-sm">
            Early skeleton for platform merchants. This page is safe to wire up while we
            build the rest of the flow.
          </p>
        </div>
        {probing && (
          <div className="text-xs text-gray-500">Checking backend feature flag…</div>
        )}
      </div>

      {!featureEnabled && (
        <div className="bg-yellow-50 border border-yellow-200 text-yellow-900 text-sm rounded-lg p-4">
          Backend feature flag for v2 platform onboarding is currently disabled.
          Front-end is live, but API endpoints will return 404 until
          <code className="mx-1 bg-yellow-100 px-1 rounded">FEATURE_PLATFORM_ONBOARDING_V2=true</code>
          is set and the backend is redeployed.
        </div>
      )}

      <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6">
        <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-4">
          <div className="flex flex-1 justify-between md:justify-start md:space-x-8">
            {STEPS.map((step) => {
              const Icon = step.icon;
              const isActive = step.id === currentStep;
              return (
                <button
                  key={step.id}
                  type="button"
                  onClick={() => setCurrentStep(step.id)}
                  className={`flex items-center space-x-3 px-3 py-2 rounded-lg border text-left transition-colors text-sm ${
                    isActive
                      ? 'border-blue-500 bg-blue-50 text-blue-700'
                      : 'border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
                  }`}
                >
                  <Icon className="w-4 h-4" />
                  <span className="font-medium">{step.title}</span>
                </button>
              );
            })}
          </div>
        </div>
      </div>

      <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6 space-y-4">
        {currentStep === 'intro' && (
          <section className="space-y-3">
            <h2 className="text-lg font-semibold text-gray-900">What is Platform Onboarding?</h2>
            <p className="text-sm text-gray-700">
              This flow is for merchants who primarily sell on external platforms (Amazon, Temu,
              etc.) or via multi-channel tools, and want to expose their catalog and payment
              capabilities to Pivota&apos;s agent network.
            </p>
            <p className="text-sm text-gray-700">
              In EPIC‑1 this page serves as a stable container, and we now support creating a
              minimal, feature-flagged platform onboarding record without touching the existing
              signup flow. Later EPICs will add:
            </p>
            <ul className="list-disc list-inside text-sm text-gray-700 space-y-1">
              <li>Registration for platform merchants separate from the existing signup flow.</li>
              <li>Selection of data sources (connectors vs. platform export files).</li>
              <li>Import status tracking and basic health signals.</li>
            </ul>
          </section>
        )}

        {currentStep === 'datasource' && (
          <section className="space-y-3">
            <h2 className="text-lg font-semibold text-gray-900">Data source (Path A / Path B)</h2>
            <p className="text-sm text-gray-700">
              When we implement the full flow, this step will let merchants choose how they
              want to connect their catalog:
            </p>
            <ul className="list-disc list-inside text-sm text-gray-700 space-y-1">
              <li>
                <strong>Path A – Multi-channel tools</strong>: connect Linnworks, ChannelAdvisor,
                or similar tools as a live data source.
              </li>
              <li>
                <strong>Path B – Platform reports</strong>: upload CSV/Excel exports from Amazon,
                Temu, etc., and map fields into Pivota&apos;s standard catalog.
              </li>
            </ul>
            <p className="text-xs text-gray-500">
              The API surface for this step is already reserved under
              <code className="mx-1 bg-gray-100 px-1 rounded">/platform-onboarding/register</code>
              so we can evolve the payload without touching the v1 onboarding routes.
            </p>
            <div className="mt-4 border-t border-slate-200 pt-4">
              <h3 className="text-sm font-semibold text-gray-900">
                Quick skeleton registration (EPIC‑1)
              </h3>
              <p className="text-xs text-gray-500 mb-3">
                This creates a side-car Platform onboarding record behind the feature flag. It does
                not affect the existing `/merchant/onboarding/*` flow.
              </p>
              <form onSubmit={handleRegister} className="space-y-3">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div>
                    <label className="block text-xs font-medium text-slate-700 mb-1">
                      Business name
                    </label>
                    <input
                      type="text"
                      required
                      value={formData.business_name}
                      onChange={(e) =>
                        setFormData((prev) => ({ ...prev, business_name: e.target.value }))
                      }
                      className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                      placeholder="e.g., Platform Merchant Inc."
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-slate-700 mb-1">
                      Region
                    </label>
                    <select
                      value={formData.region}
                      onChange={(e) =>
                        setFormData((prev) => ({ ...prev, region: e.target.value }))
                      }
                      className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                    >
                      <option value="US">United States</option>
                      <option value="CA">Canada</option>
                      <option value="UK">United Kingdom</option>
                      <option value="EU">European Union</option>
                      <option value="APAC">Asia Pacific</option>
                      <option value="Other">Other</option>
                    </select>
                  </div>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div>
                    <label className="block text-xs font-medium text-slate-700 mb-1">
                      Contact email (optional)
                    </label>
                    <input
                      type="email"
                      value={formData.contact_email}
                      onChange={(e) =>
                        setFormData((prev) => ({ ...prev, contact_email: e.target.value }))
                      }
                      className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                      placeholder="you@example.com"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-slate-700 mb-1">
                      Store URL or profile URL (optional)
                    </label>
                    <input
                      type="text"
                      value={formData.store_url}
                      onChange={(e) =>
                        setFormData((prev) => ({ ...prev, store_url: e.target.value }))
                      }
                      className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                      placeholder="https://amazon.com/your-brand, https://temu.com/shop/..."
                    />
                  </div>
                </div>

                <div>
                  <label className="block text-xs font-medium text-slate-700 mb-1">
                    Source type
                  </label>
                  <div className="flex gap-3 text-xs">
                    {[
                      { id: 'connector', label: 'Connector tools' },
                      { id: 'report', label: 'Platform reports' },
                      { id: 'unknown', label: 'Not decided yet' },
                    ].map((opt) => (
                      <button
                        key={opt.id}
                        type="button"
                        onClick={() =>
                          setFormData((prev) => ({ ...prev, source_type: opt.id }))
                        }
                        className={`px-3 py-1 rounded-full border ${
                          formData.source_type === opt.id
                            ? 'border-blue-500 bg-blue-50 text-blue-700'
                            : 'border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
                        }`}
                      >
                        {opt.label}
                      </button>
                    ))}
                  </div>
                </div>

                {submitError && (
                  <div className="text-xs text-red-600 bg-red-50 border border-red-200 rounded-md px-3 py-2">
                    {submitError}
                  </div>
                )}

                {submitResult && (
                  <div className="text-xs bg-green-50 border border-green-200 rounded-md px-3 py-2 space-y-2">
                    <div className="font-semibold text-green-700">Onboarding record created</div>
                    <div className="text-green-800">
                      <div>onboarding_id: {submitResult.onboarding_id}</div>
                      <div>status: {submitResult.status}</div>
                      {submitResult.platform_profile && (
                        <div className="text-[11px]">
                          source_type: {submitResult.platform_profile.source_type}
                        </div>
                      )}
                    </div>
                    {importTasks && importTasks.length > 0 && (
                      <div className="mt-2 border-t border-green-100 pt-2">
                        <div className="flex items-center justify-between mb-1">
                          <div className="font-semibold text-[11px] text-green-700">
                            Recent import tasks
                          </div>
                          <button
                            type="button"
                            onClick={handleRefreshTasks}
                            className="text-[10px] text-blue-600 hover:text-blue-700 underline-offset-2 hover:underline"
                          >
                            Refresh
                          </button>
                        </div>
                        <ul className="space-y-1">
                          {importTasks.map((t) => (
                            <li key={t.id} className="flex items-center justify-between text-[11px]">
                              <span>
                                #{t.id} · {t.connector || 'n/a'} · {t.source_type}
                                {t.counts?.total != null && (
                                  <> · {t.counts.total} items</>
                                )}
                                {t.counts?.pages_fetched != null && (
                                  <>
                                    {' '}
                                    · {t.counts.pages_fetched} page
                                    {t.counts.pages_fetched === 1 ? '' : 's'}
                                  </>
                                )}
                              </span>
                              <span className="text-right">
                                <span className="capitalize">{t.status}</span>
                                {t.counts?.duration_sec != null && (
                                  <span className="ml-1 text-[10px] text-slate-500">
                                    ({t.counts.duration_sec.toFixed?.(2) ?? t.counts.duration_sec}s)
                                  </span>
                                )}
                                {t.status === 'failed' && t.counts?.error_type && (
                                  <span className="ml-1 text-[10px] text-red-600">
                                    [
                                    {t.counts.error_type}
                                    {t.counts.error_category
                                      ? `/${t.counts.error_category}`
                                      : ''}
                                    ]
                                  </span>
                                )}
                              </span>
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                  </div>
                )}

                <button
                  type="submit"
                  disabled={!featureEnabled || submitting}
                  className="inline-flex items-center px-4 py-2 rounded-md text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {submitting ? 'Creating…' : 'Create platform onboarding record'}
                </button>
              </form>
            </div>
          </section>
        )}

        {currentStep === 'reports' && (
          <section className="space-y-4">
            <h2 className="text-lg font-semibold text-gray-900">Platform reports (Amazon / Temu)</h2>
            <p className="text-sm text-gray-700">
              After your platform onboarding record is created, you can upload CSV reports from
              Amazon or Temu. We will validate the file and import products into your catalog cache.
            </p>
            {!onboardingId && (
              <p className="text-xs text-red-600">
                Please complete the registration step first. Once an onboarding_id is created, you
                can upload reports here.
              </p>
            )}

            {onboardingId && (
              <div className="space-y-4">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div>
                    <label className="block text-xs font-medium text-slate-700 mb-1">
                      Report type
                    </label>
                    <select
                      value={reportType}
                      onChange={(e) => {
                        setReportType(e.target.value as 'amazon' | 'temu');
                        setValidateResult(null);
                      }}
                      className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                    >
                      <option value="amazon">Amazon</option>
                      <option value="temu">Temu</option>
                    </select>
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-slate-700 mb-1">
                      CSV file
                    </label>
                    <input
                      type="file"
                      accept=".csv"
                      onChange={(e) => {
                        const f = e.target.files?.[0] || null;
                        setFile(f);
                        setValidateResult(null);
                      }}
                      className="block w-full text-sm text-slate-700 file:mr-3 file:py-1.5 file:px-3 file:rounded-md file:border file:border-slate-300 file:text-xs file:font-medium file:bg-white file:text-slate-700 hover:file:bg-slate-50"
                    />
                    <p className="mt-1 text-[11px] text-slate-500">
                      We only read a small preview for validation. Full data is stored after you
                      choose to import.
                    </p>
                  </div>
                </div>

                <div className="flex gap-3">
                  <button
                    type="button"
                    disabled={!onboardingId || !file || validating}
                    onClick={handleValidateReport}
                    className="inline-flex items-center px-4 py-2 rounded-md text-xs font-medium text-blue-700 bg-blue-50 hover:bg-blue-100 disabled:opacity-50"
                  >
                    {validating ? 'Validating…' : 'Validate CSV'}
                  </button>
                  <button
                    type="button"
                    disabled={
                      !onboardingId || !file || !validateResult?.ready_to_import || uploading
                    }
                    onClick={handleUploadReport}
                    className="inline-flex items-center px-4 py-2 rounded-md text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-50"
                  >
                    {uploading ? 'Importing…' : 'Import and start sync'}
                  </button>
                  <button
                    type="button"
                    disabled={!onboardingId || productsLoading}
                    onClick={handleRefreshProducts}
                    className="inline-flex items-center px-4 py-2 rounded-md text-xs font-medium text-slate-700 bg-slate-50 hover:bg-slate-100 disabled:opacity-50"
                  >
                    {productsLoading ? 'Loading products…' : 'Refresh orderable products'}
                  </button>
                </div>

                {validateResult && (
                  <div className="border border-slate-200 rounded-lg p-3 space-y-2 text-xs">
                    <div className="flex items-center justify-between">
                      <span className="font-semibold text-slate-800">Validation result</span>
                      <span
                        className={`px-2 py-0.5 rounded-full ${
                          validateResult.ready_to_import
                            ? 'bg-emerald-50 text-emerald-700'
                            : 'bg-amber-50 text-amber-700'
                        }`}
                      >
                        {validateResult.ready_to_import ? 'Ready to import' : 'Needs fixes'}
                      </span>
                    </div>
                    {validateResult.missing_columns.length > 0 && (
                      <p className="text-amber-700">
                        Missing required columns: {validateResult.missing_columns.join(', ')}
                      </p>
                    )}
                    <p className="text-slate-600">
                      Scanned {validateResult.rows_scanned} rows.
                      {validateResult.rows_with_missing_required_fields > 0 && (
                        <span className="text-amber-700">
                          {' '}
                          Found {validateResult.rows_with_missing_required_fields} rows with missing
                          required fields.
                        </span>
                      )}
                    </p>
                  </div>
                )}

                {products.length > 0 && (
                  <div className="border border-slate-200 rounded-lg p-3 text-xs space-y-2">
                    <div className="flex items-center justify-between">
                      <span className="font-semibold text-slate-800">
                        Orderable products preview ({products.length})
                      </span>
                    </div>
                    <div className="overflow-x-auto">
                      <table className="min-w-full text-left text-[11px]">
                        <thead>
                          <tr className="border-b border-slate-200 bg-slate-50">
                            <th className="px-2 py-1 font-medium text-slate-700">ID</th>
                            <th className="px-2 py-1 font-medium text-slate-700">Title</th>
                            <th className="px-2 py-1 font-medium text-slate-700">Price</th>
                            <th className="px-2 py-1 font-medium text-slate-700">Orderable</th>
                          </tr>
                        </thead>
                        <tbody>
                          {products.map((p) => {
                            const isOrderable =
                              p.orderable === true ||
                              p.orderable_validation?.orderable === true;
                            const errors = p.orderable_validation?.errors || [];
                            return (
                              <tr key={p.id} className="border-b border-slate-100">
                                <td className="px-2 py-1 text-slate-700 truncate max-w-[120px]">
                                  {p.id}
                                </td>
                                <td className="px-2 py-1 text-slate-700 truncate max-w-[200px]">
                                  {p.title || '(no title)'}
                                </td>
                                <td className="px-2 py-1 text-slate-700">
                                  {p.price} {p.currency}
                                </td>
                                <td className="px-2 py-1">
                                  {isOrderable ? (
                                    <span className="inline-flex items-center px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700">
                                      Ready to order
                                    </span>
                                  ) : (
                                    <span className="inline-flex items-center px-2 py-0.5 rounded-full bg-amber-50 text-amber-700">
                                      Needs data
                                    </span>
                                  )}
                                  {!isOrderable && errors.length > 0 && (
                                    <div className="mt-1 text-[10px] text-slate-500">
                                      {errors.join(', ')}
                                    </div>
                                  )}
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}

                {onboardingId && (
                  <div className="border border-slate-200 rounded-lg p-3 text-xs space-y-2">
                    <div className="flex items-center justify-between">
                      <span className="font-semibold text-slate-800">
                        Test stub order (optional)
                      </span>
                      <span className="text-[11px] text-slate-500">
                        Uses {reportType === 'amazon' ? 'Amazon' : 'Temu'} stub mode – no real
                        order is sent.
                      </span>
                    </div>
                    <div className="grid grid-cols-1 md:grid-cols-4 gap-2">
                      <div className="md:col-span-2">
                        <label className="block text-[11px] font-medium text-slate-700 mb-1">
                          Product ID
                        </label>
                        <input
                          type="text"
                          value={orderProductId}
                          onChange={(e) => setOrderProductId(e.target.value)}
                          className="w-full px-2 py-1.5 border border-slate-300 rounded-md text-xs focus:ring-1 focus:ring-blue-500 focus:border-blue-500"
                          placeholder="Copy from table above, e.g. B08… or TEMU-…"
                        />
                      </div>
                      <div>
                        <label className="block text-[11px] font-medium text-slate-700 mb-1">
                          Variant ID (optional)
                        </label>
                        <input
                          type="text"
                          value={orderVariantId}
                          onChange={(e) => setOrderVariantId(e.target.value)}
                          className="w-full px-2 py-1.5 border border-slate-300 rounded-md text-xs focus:ring-1 focus:ring-blue-500 focus:border-blue-500"
                          placeholder="Variant ID if available"
                        />
                      </div>
                      <div>
                        <label className="block text-[11px] font-medium text-slate-700 mb-1">
                          Quantity
                        </label>
                        <input
                          type="number"
                          min={1}
                          max={10}
                          value={orderQuantity}
                          onChange={(e) => setOrderQuantity(parseInt(e.target.value || '1', 10))}
                          className="w-full px-2 py-1.5 border border-slate-300 rounded-md text-xs focus:ring-1 focus:ring-blue-500 focus:border-blue-500"
                        />
                      </div>
                    </div>
                    <div className="flex items-center justify-between mt-2">
                      <button
                        type="button"
                        disabled={!orderProductId.trim() || ordering}
                        onClick={handleCreateStubOrder}
                        className="inline-flex items-center px-3 py-1.5 rounded-md text-xs font-medium text-white bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50"
                      >
                        {ordering ? 'Creating…' : 'Create test order (stub)'}
                      </button>
                    </div>
                    {orderResult && (
                      <div className="mt-2 border-t border-slate-200 pt-2 space-y-1">
                        <div className="flex items-center justify-between">
                          <span className="font-semibold text-slate-800">Last test result</span>
                          <span className="text-[11px] text-slate-500">
                            Status: {orderResult.status} · Mode: {orderResult.poc_mode || 'stub'}
                          </span>
                        </div>
                        <div className="text-[11px] text-slate-700">
                          <div>Platform order ID: {orderResult.platform_order_id || 'n/a'}</div>
                          <div>Platform: {orderResult.platform}</div>
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}
          </section>
        )}

        {currentStep === 'coming-soon' && (
          <section className="space-y-3">
            <h2 className="text-lg font-semibold text-gray-900">Coming next</h2>
            <p className="text-sm text-gray-700">
              The next EPICs will add the actual catalog import pipeline, MDQS calculation and
              agent visibility controls. This page will be extended to:
            </p>
            <ul className="list-disc list-inside text-sm text-gray-700 space-y-1">
              <li>Show ImportTask status (pending/running/succeeded/failed).</li>
              <li>Surface PSG/MDQS quality scores at a glance.</li>
              <li>Let you choose which SKUs are visible to agents and toggle the agent channel.</li>
            </ul>
            <p className="text-xs text-gray-500">
              Because this skeleton is feature-flagged and uses dedicated endpoints, it is safe to
              ship early without impacting existing merchants.
            </p>
          </section>
        )}
      </div>
    </div>
  );
}
