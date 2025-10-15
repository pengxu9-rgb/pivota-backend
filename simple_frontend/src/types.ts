export interface User {
  id: string;
  email: string;
  role: string;
  approved: boolean;
  created_at: string;
}

export interface PSP {
  id: string;
  name: string;
  type: string;
  status: string;
  enabled: boolean;
  sandbox_mode: boolean;
  connection_health: string;
  api_response_time: number;
  last_tested: string;
  test_results: {
    success: boolean;
    response_time_ms: number;
    [key: string]: any;
  };
}

export interface RoutingRule {
  id: string;
  name: string;
  rule_type: string;
  conditions: Record<string, any>;
  target_psp: string;
  enabled: boolean;
  priority: number;
  created_at: string;
  performance?: {
    success_rate: number;
    avg_latency: number;
    total_transactions: number;
  };
}

export interface Merchant {
  id: string;
  name: string;
  platform: string;
  store_url: string;
  status: string;
  created_at: string;
  integration_data: Record<string, any>;
  kyb_documents: Array<{
    type: string;
    status: string;
    uploaded_at: string;
  }>;
  verification_status: string;
  volume_processed: number;
  last_activity: string;
}

export interface SystemLog {
  id: string;
  timestamp: string;
  level: string;
  action: string;
  message: string;
  details: Record<string, any>;
}

export interface ApiKey {
  id: string;
  name: string;
  permissions: string[];
  created_at: string;
  last_used: string;
  usage_count: number;
  usage_rate: number;
  days_since_creation: number;
  enabled: boolean;
  created_by: string;
}

export interface Analytics {
  period_days: number;
  timestamp: string;
  system_metrics: {
    total_payments: number;
    successful_payments: number;
    failed_payments: number;
    success_rate: number;
    active_agents: number;
    total_agents: number;
    active_merchants: number;
    total_merchants: number;
  };
  psp_performance: Record<string, any>;
  routing_usage: Record<string, any>;
  kyb_metrics: {
    total_merchants: number;
    approved_rate: number;
  };
  admin_actions: {
    total_logs: number;
    recent_actions: number;
    actions_per_day: number;
  };
  data_source: string;
}


