'use client';

import { useEffect, useMemo, useState } from 'react';
import { FEATURE_FLAGS } from '@/lib/config';
import { platformOnboardingApi } from '@/lib/api';
import { RefreshCw, Upload, CheckCircle2, XCircle, Activity, Package, Play } from 'lucide-react';

type Platform = 'amazon' | 'temu';

interface ValidatePreviewRow {
  row_number: number;
  row_data: Record<string, string>;
  missing_required_fields: string[];
  valid: boolean;
}

interface ValidateResponse {
  status: string;
  onboarding_id: string;
  platform: Platform;
  header: string[];
  required_columns: string[];
  missing_columns: string[];
  preview: ValidatePreviewRow[];
  issues: {
    missing_columns: string[];
    rows_scanned: number;
    preview_rows: number;
    rows_with_missing_required_fields: number;
  };
  ready_to_import: boolean;
}

export default function PlatformOrdersPage() {
  const flagEnabled = FEATURE_FLAGS.PLATFORM_ORDERS_V1;

  const [onboardingId, setOnboardingId] = useState('');
  const [platform, setPlatform] = useState<Platform>('amazon');
  const [file, setFile] = useState<File | null>(null);
  const [validating, setValidating] = useState(false);
  const [validateResult, setValidateResult] = useState<ValidateResponse | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadResult, setUploadResult] = useState<any | null>(null);

  const [orderProductId, setOrderProductId] = useState('');
  const [orderVariantId, setOrderVariantId] = useState('');
  const [orderQuantity, setOrderQuantity] = useState(1);
  const [stubOrdering, setStubOrdering] = useState(false);
  const [stubOrderResult, setStubOrderResult] = useState<any | null>(null);
  const [orders, setOrders] = useState<any[]>([]);
  const [ordersTotal, setOrdersTotal] = useState(0);
  const [ordersLoading, setOrdersLoading] = useState(false);
  const [ordersPage, setOrdersPage] = useState(1);
  const ordersPageSize = 20;
  const [acpLoading, setAcpLoading] = useState<number | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(false);

  useEffect(() => {
    // Pre-fill onboarding_id from localStorage if present
    const mid = typeof window !== 'undefined' ? localStorage.getItem('merchant_id') || '' : '';
    if (mid) {
      setOnboardingId(mid);
    }
  }, []);

  // Auto-refresh orders when payment is pending
  useEffect(() => {
    if (!autoRefresh || !onboardingId) return;
    
    const interval = setInterval(() => {
      loadOrders(ordersPage);
    }, 10000); // Refresh every 10 seconds
    
    return () => clearInterval(interval);
  }, [autoRefresh, onboardingId, ordersPage]);

  const handleSendToACP = async (order: any) => {
    setAcpLoading(order.id);
    try {
      const response = await platformOnboardingApi.createACPCheckout(order.id);
      
      if (response.status === 'already_created') {
        // Session already exists, open it
        if (response.checkout_url) {
          window.open(response.checkout_url, '_blank');
        }
        alert(`ACP session already exists: ${response.session_id}\n${response.message}`);
      } else if (response.checkout_url) {
        // New session created
        window.open(response.checkout_url, '_blank');
        
        // Show success message with instructions
        const message = `✅ ACP Checkout Created!\n\n` +
                       `Session ID: ${response.session_id}\n\n` +
                       `Checkout page opened in new tab.\n` +
                       `Complete payment to update order status.`;
        alert(message);
      } else {
        alert(`ACP session: ${response.message || response.session_id}`);
      }
      
      // Refresh orders to show updated status
      await loadOrders(ordersPage);
    } catch (err: any) {
      console.error('Failed to create ACP checkout', err);
      const errorDetail = err.response?.data?.detail || err.message;
      alert(`❌ Failed to create ACP checkout:\n\n${errorDetail}\n\nPlease try again or contact support.`);
    } finally {
      setAcpLoading(null);
    }
  };

  const getPaymentStatusBadge = (status?: string) => {
    if (!status || status === 'null') return <span className="text-gray-400 text-xs">—</span>;
    const badges: Record<string, JSX.Element> = {
      paid: <span className="px-2 py-1 bg-green-100 text-green-700 rounded text-xs font-medium">Paid</span>,
      pending: <span className="px-2 py-1 bg-yellow-100 text-yellow-700 rounded text-xs font-medium">Pending</span>,
      failed: <span className="px-2 py-1 bg-red-100 text-red-700 rounded text-xs font-medium">Failed</span>,
    };
    return badges[status] || <span className="text-gray-600 text-xs">{status}</span>;
  };

  const getFulfillmentStatusBadge = (status?: string) => {
    if (!status || status === 'null') return <span className="text-gray-400 text-xs">—</span>;
    const badges: Record<string, JSX.Element> = {
      shipped: <span className="px-2 py-1 bg-blue-100 text-blue-700 rounded text-xs font-medium">Shipped</span>,
      delivered: <span className="px-2 py-1 bg-green-100 text-green-700 rounded text-xs font-medium">Delivered</span>,
      pending: <span className="px-2 py-1 bg-gray-100 text-gray-700 rounded text-xs font-medium">Pending</span>,
    };
    return badges[status] || <span className="text-gray-600 text-xs">{status}</span>;
  };

  useEffect(() => {
    if (!flagEnabled || !onboardingId.trim()) return;
    loadOrders(ordersPage, platform);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ordersPage, platform, onboardingId, flagEnabled]);

  const canUpload = useMemo(
    () => !!file && !!onboardingId.trim() && validateResult?.ready_to_import,
    [file, onboardingId, validateResult],
  );

  const handleValidate = async () => {
    if (!file || !onboardingId.trim()) return;
    setValidating(true);
    setValidateResult(null);
    setUploadResult(null);
    try {
      const data = await platformOnboardingApi.validateOrders(onboardingId.trim(), platform, file);
      setValidateResult(data as ValidateResponse);
    } catch (err: any) {
      console.error('Orders validate failed', err);
      alert(err?.message || 'Validation failed');
    } finally {
      setValidating(false);
    }
  };

  const handleUpload = async () => {
    if (!file || !onboardingId.trim() || !validateResult?.ready_to_import) return;
    setUploading(true);
    setUploadResult(null);
    try {
      const data = await platformOnboardingApi.uploadOrders(onboardingId.trim(), platform, file);
      setUploadResult(data);
      if (data?.import_task_id) {
        alert(`Orders CSV accepted, task: ${data.import_task_id} (stub)`);
      }
      // reset file after upload
      setFile(null);
      setValidateResult(null);
    } catch (err: any) {
      console.error('Orders upload failed', err);
      alert(err?.message || 'Upload failed');
    } finally {
      setUploading(false);
    }
  };

  const handleCreateStubOrder = async () => {
    if (!onboardingId.trim() || !orderProductId.trim()) return;
    setStubOrdering(true);
    setStubOrderResult(null);
    try {
      const payload = {
        platform: orderPlatform,
        platform_product_id: orderProductId.trim(),
        variant_id: orderVariantId.trim() || undefined,
        quantity: orderQuantity > 0 ? orderQuantity : 1,
      } as const;
      const data = await platformOnboardingApi.createPlatformOrderPoc(onboardingId.trim(), payload);
      setStubOrderResult(data);
    } catch (err: any) {
      console.error('Stub order failed', err);
      alert(err?.message || 'Failed to create stub order');
    } finally {
      setStubOrdering(false);
    }
  };

  const loadOrders = async (page: number = 1, platformParam: Platform = platform) => {
    if (!onboardingId.trim()) return;
    setOrdersLoading(true);
    try {
      const resp = await platformOnboardingApi.listOrders(onboardingId.trim(), {
        platform: platformParam,
        limit: ordersPageSize,
        offset: (page - 1) * ordersPageSize,
      });
      const body = (resp as any)?.data || resp;
      setOrders(body.orders || []);
      setOrdersTotal(body.total || 0);
    } catch (err) {
      console.error('Load orders failed', err);
      setOrders([]);
      setOrdersTotal(0);
    } finally {
      setOrdersLoading(false);
    }
  };

  if (!flagEnabled) {
    return (
      <div className="max-w-4xl mx-auto space-y-3">
        <h1 className="text-2xl font-bold text-gray-900">Platform Orders (Preview)</h1>
        <p className="text-sm text-gray-600">
          This feature is disabled in this environment. Set{' '}
          <code className="bg-gray-100 px-1 rounded">NEXT_PUBLIC_FEATURE_PLATFORM_ORDERS_V1=true</code>{' '}
          to enable.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6 max-w-5xl mx-auto">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Platform Orders</h1>
          <p className="text-sm text-gray-600">
            Upload and manage Amazon/Temu orders CSV imports.
          </p>
        </div>
      </div>

      {/* Statistics Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="bg-white shadow-sm border border-slate-200 rounded-lg p-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-xs font-medium text-slate-600 uppercase">Total Orders</p>
              <p className="text-2xl font-bold text-slate-900 mt-1">
                {ordersLoading ? '...' : ordersTotal || 0}
              </p>
            </div>
            <Package className="w-8 h-8 text-blue-500 opacity-50" />
          </div>
        </div>

        <div className="bg-white shadow-sm border border-slate-200 rounded-lg p-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-xs font-medium text-slate-600 uppercase">Last Import</p>
              <p className="text-sm font-semibold text-slate-900 mt-1">
                {ordersLoading ? '...' : orders.length > 0 && orders[0]?.created_at
                  ? new Date(orders[0].created_at + 'Z').toLocaleString()
                  : 'No imports yet'}
              </p>
            </div>
            <Activity className="w-8 h-8 text-green-500 opacity-50" />
          </div>
        </div>

        <div className="bg-white shadow-sm border border-slate-200 rounded-lg p-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-xs font-medium text-slate-600 uppercase">Current Platform</p>
              <p className="text-lg font-bold text-slate-900 mt-1 capitalize">
                {platform}
              </p>
              <p className="text-xs text-slate-500 mt-1">
                {ordersLoading ? '...' : `${ordersTotal || 0} orders`}
              </p>
            </div>
            <CheckCircle2 className="w-8 h-8 text-purple-500 opacity-50" />
          </div>
        </div>
      </div>

      <div className="bg-white shadow-sm border border-slate-200 rounded-lg p-4 space-y-4">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <label className="block text-xs font-medium text-slate-700 mb-1">Onboarding ID</label>
            <input
              type="text"
              value={onboardingId}
              onChange={(e) => setOnboardingId(e.target.value)}
              className="w-full px-3 py-2 border rounded-lg text-sm"
              placeholder="merch_xxx..."
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-slate-700 mb-1">Platform</label>
            <select
              value={platform}
              onChange={(e) => {
                setPlatform(e.target.value as Platform);
                setValidateResult(null);
                setUploadResult(null);
              }}
              className="w-full px-3 py-2 border rounded-lg text-sm"
            >
              <option value="amazon">Amazon</option>
              <option value="temu">Temu</option>
            </select>
          </div>
          <div className="flex items-end">
            <button
              onClick={() => {
                setFile(null);
                setValidateResult(null);
                setUploadResult(null);
              }}
              className="px-3 py-2 border rounded-lg text-sm w-full"
            >
              Reset
            </button>
          </div>
        </div>

        <div className="space-y-2">
          <label className="block text-xs font-medium text-slate-700">Orders CSV</label>
          <div className="flex items-center gap-3">
            <input
              type="file"
              accept=".csv"
              onChange={(e) => {
                const f = e.target.files?.[0] || null;
                setFile(f);
                setValidateResult(null);
                setUploadResult(null);
              }}
              className="text-sm"
            />
            <button
              onClick={handleValidate}
              disabled={!file || !onboardingId.trim() || validating}
              className="inline-flex items-center px-3 py-2 rounded-md text-xs font-medium text-blue-700 bg-blue-50 hover:bg-blue-100 disabled:opacity-50"
            >
              {validating ? <Activity className="w-4 h-4 mr-2 animate-spin" /> : <CheckCircle2 className="w-4 h-4 mr-2" />}
              Validate
            </button>
            <button
              onClick={handleUpload}
              disabled={!canUpload || uploading}
              className="inline-flex items-center px-3 py-2 rounded-md text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-50"
            >
              {uploading ? <Upload className="w-4 h-4 mr-2 animate-spin" /> : <Upload className="w-4 h-4 mr-2" />}
              Upload (stub)
            </button>
          </div>
          <p className="text-[11px] text-slate-500">
            See `PLATFORM_ORDERS_CSV_SPEC.md` for required columns. Current flow is validation-only; ingestion is deferred.
          </p>
        </div>

        {validateResult && (
          <div className="border border-slate-200 rounded-lg p-3 text-xs space-y-2">
            <div className="flex items-center justify-between">
              <span className="font-semibold text-slate-800">Validation result</span>
              <span
                className={`px-2 py-0.5 rounded-full ${
                  validateResult.ready_to_import ? 'bg-emerald-50 text-emerald-700' : 'bg-amber-50 text-amber-700'
                }`}
              >
                {validateResult.ready_to_import ? 'Ready to import' : 'Needs fixes'}
              </span>
            </div>
            {validateResult.missing_columns.length > 0 && (
              <p className="text-amber-700">Missing columns: {validateResult.missing_columns.join(', ')}</p>
            )}
            <p className="text-slate-600">
              Previewed {validateResult.issues.preview_rows} rows (scanned {validateResult.issues.rows_scanned}).
            </p>
            {validateResult.preview.length > 0 && (
              <div className="border border-slate-100 rounded p-2 bg-slate-50">
                {validateResult.preview.map((row) => (
                  <div key={row.row_number} className="flex items-start justify-between text-[11px] border-b border-slate-100 last:border-0 py-1">
                    <div>
                      <span className="font-semibold text-slate-800">Row {row.row_number}</span>{' '}
                      {!row.valid && <span className="text-amber-700">(missing: {row.missing_required_fields.join(', ')})</span>}
                    </div>
                    <div className="text-slate-500 truncate max-w-[320px]">
                      {Object.entries(row.row_data || {})
                        .slice(0, 4)
                        .map(([k, v]) => `${k}: ${v}`)
                        .join(' • ')}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {uploadResult && (
          <div className="border border-emerald-200 bg-emerald-50 rounded-lg p-3 text-xs text-emerald-800">
            Accepted. Import task (stub): {uploadResult.import_task_id || 'n/a'}
          </div>
        )}
      </div>

      {/* Temporarily disabled - POC endpoint requires admin access */}
      {false && (
        <div className="bg-white shadow-sm border border-slate-200 rounded-lg p-4 space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Package className="w-4 h-4 text-slate-700" />
            <h2 className="text-lg font-semibold text-gray-900">Create stub order (optional)</h2>
          </div>
          <span className="text-[11px] text-slate-500">
            Amazon/Temu stub only; no real fulfillment or payment.
          </span>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-5 gap-3 text-sm">
          <div>
            <label className="block text-xs font-medium text-slate-700 mb-1">Platform</label>
            <select
              value={orderPlatform}
              onChange={(e) => setOrderPlatform(e.target.value as Platform)}
              className="w-full px-3 py-2 border rounded-lg text-sm"
            >
              <option value="amazon">Amazon</option>
              <option value="temu">Temu</option>
            </select>
          </div>
          <div className="md:col-span-2">
            <label className="block text-xs font-medium text-slate-700 mb-1">Product ID</label>
            <input
              type="text"
              value={orderProductId}
              onChange={(e) => setOrderProductId(e.target.value)}
              className="w-full px-3 py-2 border rounded-lg text-sm"
              placeholder="e.g., B08N5WRWNW or PROD-12345"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-slate-700 mb-1">Variant ID</label>
            <input
              type="text"
              value={orderVariantId}
              onChange={(e) => setOrderVariantId(e.target.value)}
              className="w-full px-3 py-2 border rounded-lg text-sm"
              placeholder="e.g., SKU-001"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-slate-700 mb-1">Quantity</label>
            <input
              type="text"
              value={orderQuantity}
              onChange={(e) => {
                const val = e.target.value;
                if (val === '') {
                  setOrderQuantity(1);
                } else {
                  const num = parseInt(val, 10);
                  if (!isNaN(num) && num > 0) {
                    setOrderQuantity(num);
                  }
                }
              }}
              onBlur={() => {
                if (orderQuantity < 1) setOrderQuantity(1);
              }}
              className="w-full px-3 py-2 border rounded-lg text-sm"
              placeholder="1"
            />
          </div>
        </div>
        <div className="flex items-center gap-3">
          <button
            onClick={handleCreateStubOrder}
            disabled={!onboardingId.trim() || !orderProductId.trim() || stubOrdering}
            className="inline-flex items-center px-4 py-2 rounded-md text-sm font-medium text-white bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50"
          >
            {stubOrdering ? <RefreshCw className="w-4 h-4 mr-2 animate-spin" /> : <Play className="w-4 h-4 mr-2" />}
            Create stub order
          </button>
        </div>
        {stubOrderResult && (
          <div className="border border-slate-200 rounded-lg p-3 text-xs text-slate-700 space-y-1">
            <div className="flex items-center justify-between">
              <span className="font-semibold">Last stub order</span>
              <span className="text-[11px] text-slate-500">
                Status: {stubOrderResult.status} · Mode: {stubOrderResult.poc_mode || 'stub'}
              </span>
            </div>
            <div className="text-[11px] text-slate-700">
              <div>Platform: {stubOrderResult.platform}</div>
              <div>Platform order ID: {stubOrderResult.platform_order_id || 'n/a'}</div>
            </div>
          </div>
        )}
      </div>
      )}

      <div className="bg-white shadow-sm border border-slate-200 rounded-lg p-4 space-y-3">
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-lg font-semibold text-gray-900">Imported orders</h2>
              <p className="text-sm text-gray-600">
                Orders ingested from orders_report tasks (Amazon/Temu).
              </p>
            </div>
            <button
              onClick={() => loadOrders(1)}
              className="flex items-center space-x-2 px-3 py-2 border rounded-lg text-sm hover:bg-gray-50"
            >
              <RefreshCw className="w-4 h-4" />
              <span>Refresh</span>
            </button>
          </div>
        {ordersLoading ? (
          <div className="py-10 text-center text-gray-500">Loading orders…</div>
        ) : orders.length === 0 ? (
          <div className="py-10 text-center text-gray-500">
            No imported orders yet for {platform}. Upload an orders CSV and run the worker.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full text-left text-sm">
              <thead>
                <tr className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-600">
                  <th className="px-3 py-2">Platform</th>
                  <th className="px-3 py-2">Order ID</th>
                  <th className="px-3 py-2">Item ID</th>
                  <th className="px-3 py-2">Items</th>
                  <th className="px-3 py-2">Payment</th>
                  <th className="px-3 py-2">Fulfillment</th>
                  <th className="px-3 py-2">Created</th>
                  {FEATURE_FLAGS.PLATFORM_ORDERS_ACP && <th className="px-3 py-2">Actions</th>}
                </tr>
              </thead>
              <tbody>
                {orders.map((o) => {
                  const items = (o.data?.items || []) as any[];
                  const paymentStatus = o.data?.payment_status;
                  const fulfillmentStatus = o.data?.fulfillment_status;
                  const hasACPSession = !!o.data?.acp_session_id;
                  
                  return (
                    <tr key={`${o.platform}-${o.order_id}-${o.order_item_id || 'n'}`} className="border-b border-slate-100">
                      <td className="px-3 py-2 text-xs uppercase text-slate-700">{o.platform}</td>
                      <td className="px-3 py-2 font-mono text-xs text-slate-800">{o.order_id}</td>
                      <td className="px-3 py-2 font-mono text-xs text-slate-600">{o.order_item_id || '—'}</td>
                      <td className="px-3 py-2 text-[11px] text-slate-700">
                        {items.length === 0
                          ? '—'
                          : items
                              .map((it) => {
                                const parts = [];
                                if (it.product_id) parts.push(it.product_id);
                                if (it.variant_id) parts.push(`#${it.variant_id}`);
                                parts.push(`qty ${it.quantity}`);
                                if (it.price) parts.push(`${it.price} ${it.currency || ''}`.trim());
                                return parts.join(' · ');
                              })
                              .join(' | ')}
                      </td>
                      <td className="px-3 py-2">{getPaymentStatusBadge(paymentStatus)}</td>
                      <td className="px-3 py-2">
                        {getFulfillmentStatusBadge(fulfillmentStatus)}
                        {o.data?.tracking_number && (
                          <div className="text-[10px] text-gray-500 mt-1">
                            Track: {o.data.tracking_number}
                          </div>
                        )}
                      </td>
                      <td className="px-3 py-2 text-xs text-slate-600">
                        {o.created_at ? new Date(o.created_at).toLocaleString() : '—'}
                      </td>
                      {FEATURE_FLAGS.PLATFORM_ORDERS_ACP && (
                        <td className="px-3 py-2">
                          {paymentStatus === 'paid' ? (
                            <span className="text-green-600 text-xs">✓ Paid</span>
                          ) : hasACPSession ? (
                            <span className="text-yellow-600 text-xs">⏳ Pending</span>
                          ) : (
                            <button
                              onClick={() => handleSendToACP(o)}
                              disabled={acpLoading === o.id}
                              className="text-xs px-2 py-1 bg-blue-50 text-blue-700 rounded hover:bg-blue-100 disabled:opacity-50"
                            >
                              {acpLoading === o.id ? 'Loading...' : 'Send to ACP'}
                            </button>
                          )}
                        </td>
                      )}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <div className="flex items-center justify-between text-sm text-gray-600">
          <div>
            Showing {(ordersPage - 1) * ordersPageSize + 1}-
            {Math.min(ordersPage * ordersPageSize, ordersTotal || ordersPage * ordersPageSize)} of{' '}
            {ordersTotal || orders.length}
          </div>
          <div className="flex items-center gap-2">
            <button
              disabled={ordersPage === 1}
              onClick={() => setOrdersPage((p) => Math.max(1, p - 1))}
              className="px-3 py-1 border rounded hover:bg-gray-50 disabled:opacity-50"
            >
              Prev
            </button>
            <span className="text-xs">
              Page {ordersPage} / {Math.max(1, Math.ceil((ordersTotal || orders.length) / ordersPageSize))}
            </span>
            <button
              disabled={ordersPage * ordersPageSize >= (ordersTotal || orders.length)}
              onClick={() => setOrdersPage((p) => p + 1)}
              className="px-3 py-1 border rounded hover:bg-gray-50 disabled:opacity-50"
            >
              Next
            </button>
          </div>
        </div>
      </div>
      
      {/* Note about stub orders */}
      <div className="bg-amber-50 border border-amber-200 rounded-lg p-4 text-sm">
        <p className="text-amber-800 font-medium">
          注意：Stub Order 功能暂时不可用 / Note: Stub Order feature is temporarily unavailable
        </p>
        <p className="text-amber-700 text-xs mt-1">
          POC 订单创建端点目前仅对管理员开放。商家可以使用 CSV 上传功能来测试订单导入流程（stub 模式）。
          <br />
          The POC order creation endpoint is currently admin-only. Merchants can use the CSV upload feature to test order import flow (stub mode).
        </p>
      </div>
    </div>
  );
}
