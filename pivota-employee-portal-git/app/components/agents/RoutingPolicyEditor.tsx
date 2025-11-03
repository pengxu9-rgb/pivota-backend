'use client';

/**
 * [Phase 4++] RoutingPolicyEditor Component
 * Edit merchant and agent routing policies with PSP management
 */

import React, { useState, useEffect } from 'react';
import { 
  Settings, 
  Save, 
  X,
  Plus,
  Trash2,
  AlertCircle,
  Check,
  ArrowUp,
  ArrowDown,
  Shield,
  Zap
} from 'lucide-react';
import { apiClient } from '@/lib/api-client';

interface RoutingPolicy {
  exclude: string[];
  prefer: string[];
  required?: string[]; // Only for merchants
  weights: Record<string, number>;
  failover: string[];
  priority: number;
}

interface RoutingPolicyEditorProps {
  ownerType: 'merchant' | 'agent';
  ownerId: string;
  ownerName?: string;
  onSave?: () => void;
  onCancel?: () => void;
}

const AVAILABLE_PSPS = [
  { id: 'stripe', name: 'Stripe', icon: '💳' },
  { id: 'adyen', name: 'Adyen', icon: '🏦' },
  { id: 'paypal', name: 'PayPal', icon: '💰' },
  { id: 'square', name: 'Square', icon: '🟩' },
  { id: 'checkout', name: 'Checkout.com', icon: '✓' }
];

export default function RoutingPolicyEditor({
  ownerType,
  ownerId,
  ownerName,
  onSave,
  onCancel
}: RoutingPolicyEditorProps) {
  const [policy, setPolicy] = useState<RoutingPolicy>({
    exclude: [],
    prefer: [],
    required: [],
    weights: {},
    failover: [],
    priority: 1
  });
  
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const [hasChanges, setHasChanges] = useState(false);

  // Fetch existing policy
  const fetchPolicy = async () => {
    setLoading(true);
    setError(null);

    try {
      const response = await apiClient.get(
        `/employee/routing/policies/${ownerType}/${ownerId}`
      );
      
      if (response.data?.policy) {
        setPolicy(response.data.policy);
      }
    } catch (err: any) {
      // 404 is expected if no policy exists yet
      if (err.response?.status !== 404) {
        setError(err.response?.data?.detail || 'Failed to load policy');
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchPolicy();
  }, [ownerType, ownerId]);

  // Save policy
  const savePolicy = async () => {
    setSaving(true);
    setError(null);
    setSuccess(false);

    try {
      await apiClient.post(
        `/employee/routing/policies/${ownerType}/${ownerId}`,
        policy
      );
      
      setSuccess(true);
      setHasChanges(false);
      
      if (onSave) {
        onSave();
      }
      
      // Hide success message after 3 seconds
      setTimeout(() => setSuccess(false), 3000);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to save policy');
    } finally {
      setSaving(false);
    }
  };

  // Update policy and mark as changed
  const updatePolicy = (updates: Partial<RoutingPolicy>) => {
    setPolicy(prev => ({ ...prev, ...updates }));
    setHasChanges(true);
  };

  // Add PSP to a list
  const addToList = (listName: keyof RoutingPolicy, psp: string) => {
    const list = policy[listName] as string[];
    if (!list.includes(psp)) {
      updatePolicy({ [listName]: [...list, psp] });
    }
  };

  // Remove PSP from a list
  const removeFromList = (listName: keyof RoutingPolicy, psp: string) => {
    const list = policy[listName] as string[];
    updatePolicy({ [listName]: list.filter(p => p !== psp) });
  };

  // Reorder PSP in preference list
  const reorderPreference = (psp: string, direction: 'up' | 'down') => {
    const index = policy.prefer.indexOf(psp);
    if (index === -1) return;

    const newIndex = direction === 'up' ? index - 1 : index + 1;
    if (newIndex < 0 || newIndex >= policy.prefer.length) return;

    const newPrefer = [...policy.prefer];
    [newPrefer[index], newPrefer[newIndex]] = [newPrefer[newIndex], newPrefer[index]];
    updatePolicy({ prefer: newPrefer });
  };

  // Update PSP weight
  const updateWeight = (psp: string, weight: number) => {
    updatePolicy({
      weights: {
        ...policy.weights,
        [psp]: Math.max(0, Math.min(1, weight))
      }
    });
  };

  return (
    <div className="bg-white rounded-lg shadow-lg p-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h3 className="text-xl font-semibold text-gray-900">
            [Phase 4++] Routing Policy Editor
          </h3>
          <p className="text-sm text-gray-600 mt-1">
            {ownerType === 'merchant' ? 'Merchant' : 'Agent'}: {ownerName || ownerId}
          </p>
        </div>
        
        <div className="flex items-center gap-2">
          {hasChanges && (
            <span className="text-xs text-orange-600 flex items-center gap-1">
              <AlertCircle className="h-3 w-3" />
              Unsaved changes
            </span>
          )}
        </div>
      </div>

      {/* Error/Success Messages */}
      {error && (
        <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-red-700 text-sm">
          {error}
        </div>
      )}
      
      {success && (
        <div className="mb-4 p-3 bg-green-50 border border-green-200 rounded-lg text-green-700 text-sm flex items-center gap-2">
          <Check className="h-4 w-4" />
          Policy saved successfully
        </div>
      )}

      {loading ? (
        <div className="text-center py-8 text-gray-500">Loading policy...</div>
      ) : (
        <div className="space-y-6">
          {/* Exclusions */}
          <div>
            <div className="flex items-center gap-2 mb-2">
              <X className="h-4 w-4 text-red-600" />
              <label className="font-medium text-gray-700">Excluded PSPs</label>
              <span className="text-xs text-gray-500">
                (These PSPs will not be used)
              </span>
            </div>
            <div className="flex flex-wrap gap-2">
              {AVAILABLE_PSPS.map(psp => (
                <button
                  key={psp.id}
                  onClick={() => 
                    policy.exclude.includes(psp.id)
                      ? removeFromList('exclude', psp.id)
                      : addToList('exclude', psp.id)
                  }
                  className={`px-3 py-1.5 rounded-lg text-sm transition-colors ${
                    policy.exclude.includes(psp.id)
                      ? 'bg-red-100 text-red-700 border border-red-300'
                      : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                  }`}
                >
                  {psp.icon} {psp.name}
                </button>
              ))}
            </div>
          </div>

          {/* Required PSPs (Merchant only) */}
          {ownerType === 'merchant' && (
            <div>
              <div className="flex items-center gap-2 mb-2">
                <Shield className="h-4 w-4 text-purple-600" />
                <label className="font-medium text-gray-700">Required PSPs</label>
                <span className="text-xs text-gray-500">
                  (Only these PSPs can be used)
                </span>
              </div>
              <div className="flex flex-wrap gap-2">
                {AVAILABLE_PSPS.map(psp => (
                  <button
                    key={psp.id}
                    onClick={() => 
                      policy.required?.includes(psp.id)
                        ? removeFromList('required', psp.id)
                        : addToList('required', psp.id)
                    }
                    disabled={policy.exclude.includes(psp.id)}
                    className={`px-3 py-1.5 rounded-lg text-sm transition-colors ${
                      policy.required?.includes(psp.id)
                        ? 'bg-purple-100 text-purple-700 border border-purple-300'
                        : policy.exclude.includes(psp.id)
                        ? 'bg-gray-50 text-gray-400 cursor-not-allowed'
                        : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                    }`}
                  >
                    {psp.icon} {psp.name}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Preferences (Ordered) */}
          <div>
            <div className="flex items-center gap-2 mb-2">
              <Zap className="h-4 w-4 text-blue-600" />
              <label className="font-medium text-gray-700">Preferred PSPs</label>
              <span className="text-xs text-gray-500">
                (In priority order)
              </span>
            </div>
            
            {policy.prefer.length > 0 ? (
              <div className="space-y-2">
                {policy.prefer.map((pspId, index) => {
                  const psp = AVAILABLE_PSPS.find(p => p.id === pspId);
                  if (!psp) return null;
                  
                  return (
                    <div
                      key={pspId}
                      className="flex items-center gap-2 p-2 bg-blue-50 rounded-lg"
                    >
                      <span className="text-sm font-medium text-blue-700 w-6">
                        {index + 1}.
                      </span>
                      <span className="flex-1 text-sm">
                        {psp.icon} {psp.name}
                      </span>
                      <div className="flex items-center gap-1">
                        <button
                          onClick={() => reorderPreference(pspId, 'up')}
                          disabled={index === 0}
                          className="p-1 hover:bg-blue-100 rounded disabled:opacity-50"
                        >
                          <ArrowUp className="h-3 w-3" />
                        </button>
                        <button
                          onClick={() => reorderPreference(pspId, 'down')}
                          disabled={index === policy.prefer.length - 1}
                          className="p-1 hover:bg-blue-100 rounded disabled:opacity-50"
                        >
                          <ArrowDown className="h-3 w-3" />
                        </button>
                        <button
                          onClick={() => removeFromList('prefer', pspId)}
                          className="p-1 hover:bg-red-100 rounded text-red-600"
                        >
                          <X className="h-3 w-3" />
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="text-sm text-gray-500">No preferences set</p>
            )}
            
            {/* Add to preferences */}
            <div className="mt-2 flex gap-2">
              {AVAILABLE_PSPS.filter(
                psp => !policy.prefer.includes(psp.id) && !policy.exclude.includes(psp.id)
              ).map(psp => (
                <button
                  key={psp.id}
                  onClick={() => addToList('prefer', psp.id)}
                  className="px-2 py-1 text-xs bg-gray-100 hover:bg-gray-200 rounded"
                >
                  <Plus className="h-3 w-3 inline" /> {psp.name}
                </button>
              ))}
            </div>
          </div>

          {/* Weights (Agent preference) */}
          {ownerType === 'agent' && (
            <div>
              <div className="flex items-center gap-2 mb-2">
                <Settings className="h-4 w-4 text-green-600" />
                <label className="font-medium text-gray-700">PSP Weights</label>
                <span className="text-xs text-gray-500">
                  (0.0 - 1.0, higher is better)
                </span>
              </div>
              
              <div className="space-y-2">
                {AVAILABLE_PSPS.filter(psp => !policy.exclude.includes(psp.id)).map(psp => (
                  <div key={psp.id} className="flex items-center gap-3">
                    <span className="text-sm w-32">
                      {psp.icon} {psp.name}
                    </span>
                    <input
                      type="range"
                      min="0"
                      max="100"
                      value={(policy.weights[psp.id] || 0.5) * 100}
                      onChange={(e) => updateWeight(psp.id, Number(e.target.value) / 100)}
                      className="flex-1"
                    />
                    <input
                      type="number"
                      min="0"
                      max="1"
                      step="0.1"
                      value={policy.weights[psp.id] || 0.5}
                      onChange={(e) => updateWeight(psp.id, Number(e.target.value))}
                      className="w-16 px-2 py-1 text-sm border rounded"
                    />
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Priority */}
          <div>
            <div className="flex items-center gap-2 mb-2">
              <label className="font-medium text-gray-700">Policy Priority</label>
              <span className="text-xs text-gray-500">
                (1-10, lower number = higher priority)
              </span>
            </div>
            <input
              type="number"
              min="1"
              max="10"
              value={policy.priority}
              onChange={(e) => updatePolicy({ priority: Number(e.target.value) })}
              className="w-24 px-3 py-1.5 border rounded-lg"
            />
          </div>

          {/* Actions */}
          <div className="flex items-center justify-end gap-3 pt-4 border-t">
            {onCancel && (
              <button
                onClick={onCancel}
                className="px-4 py-2 text-gray-600 hover:bg-gray-100 rounded-lg transition-colors"
              >
                Cancel
              </button>
            )}
            
            <button
              onClick={savePolicy}
              disabled={saving || !hasChanges}
              className={`px-4 py-2 rounded-lg flex items-center gap-2 transition-colors ${
                saving || !hasChanges
                  ? 'bg-gray-100 text-gray-400 cursor-not-allowed'
                  : 'bg-blue-600 text-white hover:bg-blue-700'
              }`}
            >
              <Save className="h-4 w-4" />
              {saving ? 'Saving...' : 'Save Policy'}
            </button>
          </div>
        </div>
      )}

      {/* Info Box */}
      <div className="mt-6 p-3 bg-gray-50 border border-gray-200 rounded-lg text-xs text-gray-600">
        <p className="font-medium mb-1">Policy Resolution Rules:</p>
        <ul className="list-disc list-inside space-y-0.5">
          <li>Merchant exclusions always override agent preferences</li>
          <li>Required PSPs (merchant only) limit choices to specified PSPs</li>
          <li>Agent weights optimize selection within allowed PSPs</li>
          <li>Whitelisted agents can override merchant rules</li>
        </ul>
      </div>
    </div>
  );
}

/**
 * [Phase 4++] RoutingPolicyEditor Component
 * Edit merchant and agent routing policies with PSP management
 */

import React, { useState, useEffect } from 'react';
import { 
  Settings, 
  Save, 
  X,
  Plus,
  Trash2,
  AlertCircle,
  Check,
  ArrowUp,
  ArrowDown,
  Shield,
  Zap
} from 'lucide-react';
import { apiClient } from '@/lib/api-client';

interface RoutingPolicy {
  exclude: string[];
  prefer: string[];
  required?: string[]; // Only for merchants
  weights: Record<string, number>;
  failover: string[];
  priority: number;
}

interface RoutingPolicyEditorProps {
  ownerType: 'merchant' | 'agent';
  ownerId: string;
  ownerName?: string;
  onSave?: () => void;
  onCancel?: () => void;
}

const AVAILABLE_PSPS = [
  { id: 'stripe', name: 'Stripe', icon: '💳' },
  { id: 'adyen', name: 'Adyen', icon: '🏦' },
  { id: 'paypal', name: 'PayPal', icon: '💰' },
  { id: 'square', name: 'Square', icon: '🟩' },
  { id: 'checkout', name: 'Checkout.com', icon: '✓' }
];

export default function RoutingPolicyEditor({
  ownerType,
  ownerId,
  ownerName,
  onSave,
  onCancel
}: RoutingPolicyEditorProps) {
  const [policy, setPolicy] = useState<RoutingPolicy>({
    exclude: [],
    prefer: [],
    required: [],
    weights: {},
    failover: [],
    priority: 1
  });
  
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const [hasChanges, setHasChanges] = useState(false);

  // Fetch existing policy
  const fetchPolicy = async () => {
    setLoading(true);
    setError(null);

    try {
      const response = await apiClient.get(
        `/employee/routing/policies/${ownerType}/${ownerId}`
      );
      
      if (response.data?.policy) {
        setPolicy(response.data.policy);
      }
    } catch (err: any) {
      // 404 is expected if no policy exists yet
      if (err.response?.status !== 404) {
        setError(err.response?.data?.detail || 'Failed to load policy');
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchPolicy();
  }, [ownerType, ownerId]);

  // Save policy
  const savePolicy = async () => {
    setSaving(true);
    setError(null);
    setSuccess(false);

    try {
      await apiClient.post(
        `/employee/routing/policies/${ownerType}/${ownerId}`,
        policy
      );
      
      setSuccess(true);
      setHasChanges(false);
      
      if (onSave) {
        onSave();
      }
      
      // Hide success message after 3 seconds
      setTimeout(() => setSuccess(false), 3000);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to save policy');
    } finally {
      setSaving(false);
    }
  };

  // Update policy and mark as changed
  const updatePolicy = (updates: Partial<RoutingPolicy>) => {
    setPolicy(prev => ({ ...prev, ...updates }));
    setHasChanges(true);
  };

  // Add PSP to a list
  const addToList = (listName: keyof RoutingPolicy, psp: string) => {
    const list = policy[listName] as string[];
    if (!list.includes(psp)) {
      updatePolicy({ [listName]: [...list, psp] });
    }
  };

  // Remove PSP from a list
  const removeFromList = (listName: keyof RoutingPolicy, psp: string) => {
    const list = policy[listName] as string[];
    updatePolicy({ [listName]: list.filter(p => p !== psp) });
  };

  // Reorder PSP in preference list
  const reorderPreference = (psp: string, direction: 'up' | 'down') => {
    const index = policy.prefer.indexOf(psp);
    if (index === -1) return;

    const newIndex = direction === 'up' ? index - 1 : index + 1;
    if (newIndex < 0 || newIndex >= policy.prefer.length) return;

    const newPrefer = [...policy.prefer];
    [newPrefer[index], newPrefer[newIndex]] = [newPrefer[newIndex], newPrefer[index]];
    updatePolicy({ prefer: newPrefer });
  };

  // Update PSP weight
  const updateWeight = (psp: string, weight: number) => {
    updatePolicy({
      weights: {
        ...policy.weights,
        [psp]: Math.max(0, Math.min(1, weight))
      }
    });
  };

  return (
    <div className="bg-white rounded-lg shadow-lg p-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h3 className="text-xl font-semibold text-gray-900">
            [Phase 4++] Routing Policy Editor
          </h3>
          <p className="text-sm text-gray-600 mt-1">
            {ownerType === 'merchant' ? 'Merchant' : 'Agent'}: {ownerName || ownerId}
          </p>
        </div>
        
        <div className="flex items-center gap-2">
          {hasChanges && (
            <span className="text-xs text-orange-600 flex items-center gap-1">
              <AlertCircle className="h-3 w-3" />
              Unsaved changes
            </span>
          )}
        </div>
      </div>

      {/* Error/Success Messages */}
      {error && (
        <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-red-700 text-sm">
          {error}
        </div>
      )}
      
      {success && (
        <div className="mb-4 p-3 bg-green-50 border border-green-200 rounded-lg text-green-700 text-sm flex items-center gap-2">
          <Check className="h-4 w-4" />
          Policy saved successfully
        </div>
      )}

      {loading ? (
        <div className="text-center py-8 text-gray-500">Loading policy...</div>
      ) : (
        <div className="space-y-6">
          {/* Exclusions */}
          <div>
            <div className="flex items-center gap-2 mb-2">
              <X className="h-4 w-4 text-red-600" />
              <label className="font-medium text-gray-700">Excluded PSPs</label>
              <span className="text-xs text-gray-500">
                (These PSPs will not be used)
              </span>
            </div>
            <div className="flex flex-wrap gap-2">
              {AVAILABLE_PSPS.map(psp => (
                <button
                  key={psp.id}
                  onClick={() => 
                    policy.exclude.includes(psp.id)
                      ? removeFromList('exclude', psp.id)
                      : addToList('exclude', psp.id)
                  }
                  className={`px-3 py-1.5 rounded-lg text-sm transition-colors ${
                    policy.exclude.includes(psp.id)
                      ? 'bg-red-100 text-red-700 border border-red-300'
                      : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                  }`}
                >
                  {psp.icon} {psp.name}
                </button>
              ))}
            </div>
          </div>

          {/* Required PSPs (Merchant only) */}
          {ownerType === 'merchant' && (
            <div>
              <div className="flex items-center gap-2 mb-2">
                <Shield className="h-4 w-4 text-purple-600" />
                <label className="font-medium text-gray-700">Required PSPs</label>
                <span className="text-xs text-gray-500">
                  (Only these PSPs can be used)
                </span>
              </div>
              <div className="flex flex-wrap gap-2">
                {AVAILABLE_PSPS.map(psp => (
                  <button
                    key={psp.id}
                    onClick={() => 
                      policy.required?.includes(psp.id)
                        ? removeFromList('required', psp.id)
                        : addToList('required', psp.id)
                    }
                    disabled={policy.exclude.includes(psp.id)}
                    className={`px-3 py-1.5 rounded-lg text-sm transition-colors ${
                      policy.required?.includes(psp.id)
                        ? 'bg-purple-100 text-purple-700 border border-purple-300'
                        : policy.exclude.includes(psp.id)
                        ? 'bg-gray-50 text-gray-400 cursor-not-allowed'
                        : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                    }`}
                  >
                    {psp.icon} {psp.name}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Preferences (Ordered) */}
          <div>
            <div className="flex items-center gap-2 mb-2">
              <Zap className="h-4 w-4 text-blue-600" />
              <label className="font-medium text-gray-700">Preferred PSPs</label>
              <span className="text-xs text-gray-500">
                (In priority order)
              </span>
            </div>
            
            {policy.prefer.length > 0 ? (
              <div className="space-y-2">
                {policy.prefer.map((pspId, index) => {
                  const psp = AVAILABLE_PSPS.find(p => p.id === pspId);
                  if (!psp) return null;
                  
                  return (
                    <div
                      key={pspId}
                      className="flex items-center gap-2 p-2 bg-blue-50 rounded-lg"
                    >
                      <span className="text-sm font-medium text-blue-700 w-6">
                        {index + 1}.
                      </span>
                      <span className="flex-1 text-sm">
                        {psp.icon} {psp.name}
                      </span>
                      <div className="flex items-center gap-1">
                        <button
                          onClick={() => reorderPreference(pspId, 'up')}
                          disabled={index === 0}
                          className="p-1 hover:bg-blue-100 rounded disabled:opacity-50"
                        >
                          <ArrowUp className="h-3 w-3" />
                        </button>
                        <button
                          onClick={() => reorderPreference(pspId, 'down')}
                          disabled={index === policy.prefer.length - 1}
                          className="p-1 hover:bg-blue-100 rounded disabled:opacity-50"
                        >
                          <ArrowDown className="h-3 w-3" />
                        </button>
                        <button
                          onClick={() => removeFromList('prefer', pspId)}
                          className="p-1 hover:bg-red-100 rounded text-red-600"
                        >
                          <X className="h-3 w-3" />
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="text-sm text-gray-500">No preferences set</p>
            )}
            
            {/* Add to preferences */}
            <div className="mt-2 flex gap-2">
              {AVAILABLE_PSPS.filter(
                psp => !policy.prefer.includes(psp.id) && !policy.exclude.includes(psp.id)
              ).map(psp => (
                <button
                  key={psp.id}
                  onClick={() => addToList('prefer', psp.id)}
                  className="px-2 py-1 text-xs bg-gray-100 hover:bg-gray-200 rounded"
                >
                  <Plus className="h-3 w-3 inline" /> {psp.name}
                </button>
              ))}
            </div>
          </div>

          {/* Weights (Agent preference) */}
          {ownerType === 'agent' && (
            <div>
              <div className="flex items-center gap-2 mb-2">
                <Settings className="h-4 w-4 text-green-600" />
                <label className="font-medium text-gray-700">PSP Weights</label>
                <span className="text-xs text-gray-500">
                  (0.0 - 1.0, higher is better)
                </span>
              </div>
              
              <div className="space-y-2">
                {AVAILABLE_PSPS.filter(psp => !policy.exclude.includes(psp.id)).map(psp => (
                  <div key={psp.id} className="flex items-center gap-3">
                    <span className="text-sm w-32">
                      {psp.icon} {psp.name}
                    </span>
                    <input
                      type="range"
                      min="0"
                      max="100"
                      value={(policy.weights[psp.id] || 0.5) * 100}
                      onChange={(e) => updateWeight(psp.id, Number(e.target.value) / 100)}
                      className="flex-1"
                    />
                    <input
                      type="number"
                      min="0"
                      max="1"
                      step="0.1"
                      value={policy.weights[psp.id] || 0.5}
                      onChange={(e) => updateWeight(psp.id, Number(e.target.value))}
                      className="w-16 px-2 py-1 text-sm border rounded"
                    />
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Priority */}
          <div>
            <div className="flex items-center gap-2 mb-2">
              <label className="font-medium text-gray-700">Policy Priority</label>
              <span className="text-xs text-gray-500">
                (1-10, lower number = higher priority)
              </span>
            </div>
            <input
              type="number"
              min="1"
              max="10"
              value={policy.priority}
              onChange={(e) => updatePolicy({ priority: Number(e.target.value) })}
              className="w-24 px-3 py-1.5 border rounded-lg"
            />
          </div>

          {/* Actions */}
          <div className="flex items-center justify-end gap-3 pt-4 border-t">
            {onCancel && (
              <button
                onClick={onCancel}
                className="px-4 py-2 text-gray-600 hover:bg-gray-100 rounded-lg transition-colors"
              >
                Cancel
              </button>
            )}
            
            <button
              onClick={savePolicy}
              disabled={saving || !hasChanges}
              className={`px-4 py-2 rounded-lg flex items-center gap-2 transition-colors ${
                saving || !hasChanges
                  ? 'bg-gray-100 text-gray-400 cursor-not-allowed'
                  : 'bg-blue-600 text-white hover:bg-blue-700'
              }`}
            >
              <Save className="h-4 w-4" />
              {saving ? 'Saving...' : 'Save Policy'}
            </button>
          </div>
        </div>
      )}

      {/* Info Box */}
      <div className="mt-6 p-3 bg-gray-50 border border-gray-200 rounded-lg text-xs text-gray-600">
        <p className="font-medium mb-1">Policy Resolution Rules:</p>
        <ul className="list-disc list-inside space-y-0.5">
          <li>Merchant exclusions always override agent preferences</li>
          <li>Required PSPs (merchant only) limit choices to specified PSPs</li>
          <li>Agent weights optimize selection within allowed PSPs</li>
          <li>Whitelisted agents can override merchant rules</li>
        </ul>
      </div>
    </div>
  );
}
