'use client';

/**
 * [Phase 4++] RoutingTracePanel Component
 * Visualize routing decisions with timeline and conflict highlighting
 */

import React, { useState, useEffect } from 'react';
import { 
  GitBranch, 
  AlertTriangle, 
  CheckCircle, 
  XCircle, 
  Clock,
  Layers,
  ArrowRight,
  Info
} from 'lucide-react';
import { apiClient } from '@/lib/api-client';

interface RoutingLog {
  id: number;
  merchant_id: string | null;
  merchant_name: string | null;
  agent_id: string | null;
  agent_name: string | null;
  order_id: string | null;
  chosen_psp: string | null;
  conflict_detected: boolean;
  resolution_method: string | null;
  execution_time_ms: number | null;
  created_at: string;
  conflicts: Array<{
    type: string;
    psp: string;
    merchant_rule: string;
    agent_rule: string;
    resolution: string;
  }>;
  decision_trace?: any;
}

interface RoutingTracePanelProps {
  merchantId?: string;
  agentId?: string;
  orderId?: string;
  onConflictClick?: (conflict: any) => void;
}

export default function RoutingTracePanel({ 
  merchantId, 
  agentId, 
  orderId,
  onConflictClick 
}: RoutingTracePanelProps) {
  const [logs, setLogs] = useState<RoutingLog[]>([]);
  const [selectedLog, setSelectedLog] = useState<RoutingLog | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<'all' | 'conflicts'>('all');

  // Fetch routing logs
  const fetchLogs = async () => {
    setLoading(true);
    setError(null);

    try {
      const params = new URLSearchParams({
        ...(merchantId && { merchant_id: merchantId }),
        ...(agentId && { agent_id: agentId }),
        conflict_only: filter === 'conflicts' ? 'true' : 'false',
        days: '30',
        limit: '50'
      });

      const response = await apiClient.get(`/employee/routing/logs?${params}`);
      setLogs(response.data || []);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to fetch routing logs');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchLogs();
  }, [merchantId, agentId, filter]);

  // Render decision trace timeline
  const renderDecisionTrace = (log: RoutingLog) => {
    if (!log.decision_trace || !selectedLog || selectedLog.id !== log.id) {
      return null;
    }

    const trace = typeof log.decision_trace === 'string' 
      ? JSON.parse(log.decision_trace) 
      : log.decision_trace;

    return (
      <div className="mt-4 p-4 bg-gray-50 rounded-lg">
        <h4 className="font-medium text-gray-700 mb-3">Decision Timeline</h4>
        
        <div className="space-y-3">
          {trace.map((step: any, index: number) => (
            <div key={index} className="flex items-start gap-3">
              <div className="flex-shrink-0 mt-1">
                {step.action === 'merchant_excluded' ? (
                  <XCircle className="h-4 w-4 text-red-500" />
                ) : step.action === 'agent_excluded' ? (
                  <AlertTriangle className="h-4 w-4 text-yellow-500" />
                ) : step.step === 'initial_psps' ? (
                  <Layers className="h-4 w-4 text-blue-500" />
                ) : (
                  <CheckCircle className="h-4 w-4 text-green-500" />
                )}
              </div>
              
              <div className="flex-1">
                <div className="text-sm font-medium text-gray-700">
                  {step.step || step.action || 'Step'}
                </div>
                
                {step.psps && (
                  <div className="text-xs text-gray-500 mt-1">
                    PSPs: {step.psps.join(', ')}
                  </div>
                )}
                
                {step.reason && (
                  <div className="text-xs text-gray-500 mt-1">
                    Reason: {step.reason}
                  </div>
                )}
                
                {step.timestamp && (
                  <div className="text-xs text-gray-400 mt-1">
                    {new Date(step.timestamp).toLocaleTimeString()}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    );
  };

  // Render conflict details
  const renderConflicts = (log: RoutingLog) => {
    if (!log.conflicts || log.conflicts.length === 0) {
      return null;
    }

    return (
      <div className="mt-3 p-3 bg-yellow-50 border border-yellow-200 rounded-lg">
        <div className="flex items-center gap-2 mb-2">
          <AlertTriangle className="h-4 w-4 text-yellow-600" />
          <span className="font-medium text-yellow-800">
            Routing Conflicts Detected
          </span>
        </div>
        
        {log.conflicts.map((conflict, index) => (
          <div key={index} className="mt-2 text-sm">
            <div className="flex items-center gap-2 text-gray-700">
              <span className="font-medium">{conflict.psp}</span>
              <ArrowRight className="h-3 w-3" />
              <span className="text-gray-500">
                Merchant: {conflict.merchant_rule} | Agent: {conflict.agent_rule}
              </span>
            </div>
            <div className="text-xs text-gray-500 mt-1">
              Resolution: {conflict.resolution}
            </div>
          </div>
        ))}
      </div>
    );
  };

  return (
    <div className="space-y-4">
      {/* Header and Filters */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <GitBranch className="h-5 w-5 text-gray-600" />
          <h3 className="text-lg font-semibold">Routing Decisions</h3>
        </div>
        
        <div className="flex items-center gap-2">
          <button
            onClick={() => setFilter('all')}
            className={`px-3 py-1 rounded text-sm ${
              filter === 'all'
                ? 'bg-blue-100 text-blue-700'
                : 'text-gray-600 hover:bg-gray-100'
            }`}
          >
            All Logs
          </button>
          <button
            onClick={() => setFilter('conflicts')}
            className={`px-3 py-1 rounded text-sm ${
              filter === 'conflicts'
                ? 'bg-yellow-100 text-yellow-700'
                : 'text-gray-600 hover:bg-gray-100'
            }`}
          >
            Conflicts Only
          </button>
        </div>
      </div>

      {/* Error State */}
      {error && (
        <div className="p-4 bg-red-50 border border-red-200 rounded-lg text-red-700 text-sm">
          {error}
        </div>
      )}

      {/* Loading State */}
      {loading && (
        <div className="text-center py-8 text-gray-500">
          Loading routing logs...
        </div>
      )}

      {/* Logs List */}
      {!loading && logs.length === 0 && (
        <div className="text-center py-8 text-gray-500">
          No routing logs found
        </div>
      )}

      {!loading && logs.length > 0 && (
        <div className="space-y-3">
          {logs.map((log) => (
            <div
              key={log.id}
              className={`p-4 border rounded-lg cursor-pointer transition-colors ${
                selectedLog?.id === log.id
                  ? 'border-blue-400 bg-blue-50'
                  : 'border-gray-200 hover:border-gray-300 bg-white'
              } ${log.conflict_detected ? 'border-l-4 border-l-yellow-400' : ''}`}
              onClick={() => setSelectedLog(log.id === selectedLog?.id ? null : log)}
            >
              {/* Log Header */}
              <div className="flex items-start justify-between">
                <div className="flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-gray-900">
                      {log.chosen_psp || 'No PSP selected'}
                    </span>
                    {log.conflict_detected && (
                      <span className="px-2 py-0.5 bg-yellow-100 text-yellow-700 text-xs rounded">
                        Conflict
                      </span>
                    )}
                    {log.resolution_method && (
                      <span className="px-2 py-0.5 bg-gray-100 text-gray-600 text-xs rounded">
                        {log.resolution_method}
                      </span>
                    )}
                  </div>
                  
                  <div className="flex items-center gap-4 mt-2 text-sm text-gray-600">
                    {log.merchant_name && (
                      <span>Merchant: {log.merchant_name}</span>
                    )}
                    {log.agent_name && (
                      <span>Agent: {log.agent_name}</span>
                    )}
                    {log.order_id && (
                      <span>Order: {log.order_id}</span>
                    )}
                  </div>
                </div>
                
                <div className="text-right text-sm">
                  <div className="flex items-center gap-1 text-gray-500">
                    <Clock className="h-3 w-3" />
                    <span>{log.execution_time_ms || 0}ms</span>
                  </div>
                  <div className="text-xs text-gray-400 mt-1">
                    {new Date(log.created_at).toLocaleString()}
                  </div>
                </div>
              </div>

              {/* Conflicts */}
              {renderConflicts(log)}

              {/* Decision Trace (when expanded) */}
              {renderDecisionTrace(log)}
            </div>
          ))}
        </div>
      )}

      {/* Info Box */}
      <div className="mt-6 p-4 bg-blue-50 border border-blue-200 rounded-lg">
        <div className="flex items-start gap-2">
          <Info className="h-5 w-5 text-blue-600 flex-shrink-0 mt-0.5" />
          <div className="text-sm text-blue-800">
            <p className="font-medium mb-1">[Phase 4++] Dual-Side Routing</p>
            <p className="text-xs">
              This panel shows how PSP selection is resolved when both merchant and agent 
              have routing rules. Conflicts are highlighted and show the resolution method used.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * [Phase 4++] RoutingTracePanel Component
 * Visualize routing decisions with timeline and conflict highlighting
 */

import React, { useState, useEffect } from 'react';
import { 
  GitBranch, 
  AlertTriangle, 
  CheckCircle, 
  XCircle, 
  Clock,
  Layers,
  ArrowRight,
  Info
} from 'lucide-react';
import { apiClient } from '@/lib/api-client';

interface RoutingLog {
  id: number;
  merchant_id: string | null;
  merchant_name: string | null;
  agent_id: string | null;
  agent_name: string | null;
  order_id: string | null;
  chosen_psp: string | null;
  conflict_detected: boolean;
  resolution_method: string | null;
  execution_time_ms: number | null;
  created_at: string;
  conflicts: Array<{
    type: string;
    psp: string;
    merchant_rule: string;
    agent_rule: string;
    resolution: string;
  }>;
  decision_trace?: any;
}

interface RoutingTracePanelProps {
  merchantId?: string;
  agentId?: string;
  orderId?: string;
  onConflictClick?: (conflict: any) => void;
}

export default function RoutingTracePanel({ 
  merchantId, 
  agentId, 
  orderId,
  onConflictClick 
}: RoutingTracePanelProps) {
  const [logs, setLogs] = useState<RoutingLog[]>([]);
  const [selectedLog, setSelectedLog] = useState<RoutingLog | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<'all' | 'conflicts'>('all');

  // Fetch routing logs
  const fetchLogs = async () => {
    setLoading(true);
    setError(null);

    try {
      const params = new URLSearchParams({
        ...(merchantId && { merchant_id: merchantId }),
        ...(agentId && { agent_id: agentId }),
        conflict_only: filter === 'conflicts' ? 'true' : 'false',
        days: '30',
        limit: '50'
      });

      const response = await apiClient.get(`/employee/routing/logs?${params}`);
      setLogs(response.data || []);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to fetch routing logs');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchLogs();
  }, [merchantId, agentId, filter]);

  // Render decision trace timeline
  const renderDecisionTrace = (log: RoutingLog) => {
    if (!log.decision_trace || !selectedLog || selectedLog.id !== log.id) {
      return null;
    }

    const trace = typeof log.decision_trace === 'string' 
      ? JSON.parse(log.decision_trace) 
      : log.decision_trace;

    return (
      <div className="mt-4 p-4 bg-gray-50 rounded-lg">
        <h4 className="font-medium text-gray-700 mb-3">Decision Timeline</h4>
        
        <div className="space-y-3">
          {trace.map((step: any, index: number) => (
            <div key={index} className="flex items-start gap-3">
              <div className="flex-shrink-0 mt-1">
                {step.action === 'merchant_excluded' ? (
                  <XCircle className="h-4 w-4 text-red-500" />
                ) : step.action === 'agent_excluded' ? (
                  <AlertTriangle className="h-4 w-4 text-yellow-500" />
                ) : step.step === 'initial_psps' ? (
                  <Layers className="h-4 w-4 text-blue-500" />
                ) : (
                  <CheckCircle className="h-4 w-4 text-green-500" />
                )}
              </div>
              
              <div className="flex-1">
                <div className="text-sm font-medium text-gray-700">
                  {step.step || step.action || 'Step'}
                </div>
                
                {step.psps && (
                  <div className="text-xs text-gray-500 mt-1">
                    PSPs: {step.psps.join(', ')}
                  </div>
                )}
                
                {step.reason && (
                  <div className="text-xs text-gray-500 mt-1">
                    Reason: {step.reason}
                  </div>
                )}
                
                {step.timestamp && (
                  <div className="text-xs text-gray-400 mt-1">
                    {new Date(step.timestamp).toLocaleTimeString()}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    );
  };

  // Render conflict details
  const renderConflicts = (log: RoutingLog) => {
    if (!log.conflicts || log.conflicts.length === 0) {
      return null;
    }

    return (
      <div className="mt-3 p-3 bg-yellow-50 border border-yellow-200 rounded-lg">
        <div className="flex items-center gap-2 mb-2">
          <AlertTriangle className="h-4 w-4 text-yellow-600" />
          <span className="font-medium text-yellow-800">
            Routing Conflicts Detected
          </span>
        </div>
        
        {log.conflicts.map((conflict, index) => (
          <div key={index} className="mt-2 text-sm">
            <div className="flex items-center gap-2 text-gray-700">
              <span className="font-medium">{conflict.psp}</span>
              <ArrowRight className="h-3 w-3" />
              <span className="text-gray-500">
                Merchant: {conflict.merchant_rule} | Agent: {conflict.agent_rule}
              </span>
            </div>
            <div className="text-xs text-gray-500 mt-1">
              Resolution: {conflict.resolution}
            </div>
          </div>
        ))}
      </div>
    );
  };

  return (
    <div className="space-y-4">
      {/* Header and Filters */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <GitBranch className="h-5 w-5 text-gray-600" />
          <h3 className="text-lg font-semibold">Routing Decisions</h3>
        </div>
        
        <div className="flex items-center gap-2">
          <button
            onClick={() => setFilter('all')}
            className={`px-3 py-1 rounded text-sm ${
              filter === 'all'
                ? 'bg-blue-100 text-blue-700'
                : 'text-gray-600 hover:bg-gray-100'
            }`}
          >
            All Logs
          </button>
          <button
            onClick={() => setFilter('conflicts')}
            className={`px-3 py-1 rounded text-sm ${
              filter === 'conflicts'
                ? 'bg-yellow-100 text-yellow-700'
                : 'text-gray-600 hover:bg-gray-100'
            }`}
          >
            Conflicts Only
          </button>
        </div>
      </div>

      {/* Error State */}
      {error && (
        <div className="p-4 bg-red-50 border border-red-200 rounded-lg text-red-700 text-sm">
          {error}
        </div>
      )}

      {/* Loading State */}
      {loading && (
        <div className="text-center py-8 text-gray-500">
          Loading routing logs...
        </div>
      )}

      {/* Logs List */}
      {!loading && logs.length === 0 && (
        <div className="text-center py-8 text-gray-500">
          No routing logs found
        </div>
      )}

      {!loading && logs.length > 0 && (
        <div className="space-y-3">
          {logs.map((log) => (
            <div
              key={log.id}
              className={`p-4 border rounded-lg cursor-pointer transition-colors ${
                selectedLog?.id === log.id
                  ? 'border-blue-400 bg-blue-50'
                  : 'border-gray-200 hover:border-gray-300 bg-white'
              } ${log.conflict_detected ? 'border-l-4 border-l-yellow-400' : ''}`}
              onClick={() => setSelectedLog(log.id === selectedLog?.id ? null : log)}
            >
              {/* Log Header */}
              <div className="flex items-start justify-between">
                <div className="flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-gray-900">
                      {log.chosen_psp || 'No PSP selected'}
                    </span>
                    {log.conflict_detected && (
                      <span className="px-2 py-0.5 bg-yellow-100 text-yellow-700 text-xs rounded">
                        Conflict
                      </span>
                    )}
                    {log.resolution_method && (
                      <span className="px-2 py-0.5 bg-gray-100 text-gray-600 text-xs rounded">
                        {log.resolution_method}
                      </span>
                    )}
                  </div>
                  
                  <div className="flex items-center gap-4 mt-2 text-sm text-gray-600">
                    {log.merchant_name && (
                      <span>Merchant: {log.merchant_name}</span>
                    )}
                    {log.agent_name && (
                      <span>Agent: {log.agent_name}</span>
                    )}
                    {log.order_id && (
                      <span>Order: {log.order_id}</span>
                    )}
                  </div>
                </div>
                
                <div className="text-right text-sm">
                  <div className="flex items-center gap-1 text-gray-500">
                    <Clock className="h-3 w-3" />
                    <span>{log.execution_time_ms || 0}ms</span>
                  </div>
                  <div className="text-xs text-gray-400 mt-1">
                    {new Date(log.created_at).toLocaleString()}
                  </div>
                </div>
              </div>

              {/* Conflicts */}
              {renderConflicts(log)}

              {/* Decision Trace (when expanded) */}
              {renderDecisionTrace(log)}
            </div>
          ))}
        </div>
      )}

      {/* Info Box */}
      <div className="mt-6 p-4 bg-blue-50 border border-blue-200 rounded-lg">
        <div className="flex items-start gap-2">
          <Info className="h-5 w-5 text-blue-600 flex-shrink-0 mt-0.5" />
          <div className="text-sm text-blue-800">
            <p className="font-medium mb-1">[Phase 4++] Dual-Side Routing</p>
            <p className="text-xs">
              This panel shows how PSP selection is resolved when both merchant and agent 
              have routing rules. Conflicts are highlighted and show the resolution method used.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
