<!-- ba9e642f-0126-4f82-b15b-0bf082f86295 ff6493af-ff2e-4c48-80fb-ab85302c477a -->
# Agents Management Phase 3 - Observability & Governance

## Implementation Strategy

Split into 3 sub-phases:

- 3.1: Metrics Collection & Storage (数据层)
- 3.2: Anomaly Detection & Alerts (检测和告警)
- 3.3: Semi-Auto Governance (人工确认的治理)

Infrastructure: Database polling (no Redis), 30s refresh (no WebSocket)

---

## Phase 3.1: Metrics Collection & Storage

### Goal

Collect and store agent performance metrics for historical analysis and monitoring.

### Database Migration: 009_agent_observability.sql

**agent_metrics** (time-series performance data):

```sql
CREATE TABLE agent_metrics (
  id SERIAL PRIMARY KEY,
  agent_id VARCHAR(50) REFERENCES agents(agent_id) ON DELETE CASCADE,
  timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  avg_response_time_ms INTEGER DEFAULT 0,
  success_rate NUMERIC(5,2) DEFAULT 0,
  error_rate NUMERIC(5,2) DEFAULT 0,
  queries_per_min INTEGER DEFAULT 0,
  total_queries_count INTEGER DEFAULT 0,
  period_minutes INTEGER DEFAULT 5,
  last_seen_at TIMESTAMP WITH TIME ZONE,
  collected_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX idx_agent_metrics_agent_ts ON agent_metrics(agent_id, timestamp DESC);
CREATE INDEX idx_agent_metrics_last_seen ON agent_metrics(agent_id, last_seen_at DESC);
```

Key fields per suggestion:

- avg_response_time_ms - average API response time
- success_rate - percentage of successful calls
- error_rate - percentage of failed calls
- last_seen_at - last activity timestamp for staleness detection

**agent_alerts** (governance alerts):

```sql
CREATE TABLE agent_alerts (
  id SERIAL PRIMARY KEY,
  alert_id VARCHAR(50) UNIQUE NOT NULL,
  agent_id VARCHAR(50) REFERENCES agents(agent_id) ON DELETE CASCADE,
  alert_type VARCHAR(50) NOT NULL,
  severity VARCHAR(20) NOT NULL,
  message TEXT NOT NULL,
  metadata JSON,
  resolved BOOLEAN DEFAULT false,
  resolved_at TIMESTAMP WITH TIME ZONE,
  resolved_by VARCHAR(100),
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX idx_agent_alerts_agent ON agent_alerts(agent_id, resolved, created_at DESC);
```

**governance_actions_log** (audit trail):

```sql
CREATE TABLE governance_actions_log (
  id SERIAL PRIMARY KEY,
  action_id VARCHAR(50) UNIQUE NOT NULL,
  agent_id VARCHAR(50) REFERENCES agents(agent_id) ON DELETE CASCADE,
  action_type VARCHAR(50) NOT NULL,
  triggered_by VARCHAR(20) NOT NULL,
  executed_by VARCHAR(100),
  action_payload JSON,
  status VARCHAR(20) DEFAULT 'pending',
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  executed_at TIMESTAMP WITH TIME ZONE
);
CREATE INDEX idx_gov_actions_agent ON governance_actions_log(agent_id, status, created_at DESC);
```

### Backend: Metrics Collection Service

File: `pivota_infra/services/agent_metrics_collector.py` (new)

Functions:

- `collect_metrics_for_agent(agent_id)` - query agent_usage_logs for last 5min
- `calculate_rolling_metrics(agent_id, window_minutes=5)` - compute avg response time, error rate, qpm
- `store_metrics(agent_id, metrics)` - insert into agent_metrics table
- `collect_all_agents_metrics()` - loop all active agents

### Background Task Scheduler

File: `pivota_infra/schedulers/metrics_scheduler.py` (new)

Simple approach using FastAPI BackgroundTasks:

- Periodic task runs every 5 minutes
- Calls `collect_all_agents_metrics()`
- No Celery/Redis needed

Or add endpoint: `POST /admin/metrics/collect-now` for manual trigger during dev.

### API Endpoints

File: `pivota_infra/routes/employee_agent_mgmt.py` (extend)

New endpoints:

- `GET /agents/{id}/metrics-history?hours=24` - query agent_metrics table
- `GET /agents/{id}/alerts?resolved=false` - query agent_alerts
- `POST /admin/metrics/collect-now` - manual metrics collection trigger

---

## Phase 3.2: Anomaly Detection & Alerts

### Goal

Detect abnormal agent behavior and create alerts for review.

### Anomaly Detection Service

File: `pivota_infra/services/agent_anomaly_detector.py` (new)

Functions:

- `detect_high_error_rate(agent_id, threshold=0.1)` - check if error_rate > 10%
- `detect_high_latency(agent_id, threshold_ms=5000)` - check avg response time
- `detect_unusual_volume(agent_id)` - detect 3x spike in queries
- `create_alert(agent_id, alert_type, severity, message)` - insert into agent_alerts

Thresholds from `agent_policies` table (already exists from Phase 1).

### Alert Types

| Type | Severity | Trigger |

|------|----------|---------|

| high_error_rate | warning/critical | error_rate > policy.max_error_rate |

| high_latency | warning | avg_latency > 5000ms |

| rate_limit_exceeded | info | queries > policy.max_requests_per_minute |

| unusual_spike | warning | qpm > 3x baseline |

### API Endpoints

New endpoints in `employee_agent_mgmt.py`:

- `GET /agents/alerts?severity=critical&resolved=false` - list all unresolved alerts
- `POST /agents/alerts/{alert_id}/resolve` - mark alert as resolved
- `GET /agents/{id}/health-score` - compute overall health (0-100)

---

## Phase 3.3: Semi-Auto Governance

### Goal

Generate governance action proposals, require human approval before execution.

### Governance Workflow

```
1. Anomaly Detected → Create Alert
2. Alert Triggers → Generate Governance Action (status=pending)
3. Employee Reviews → Approve/Reject in UI
4. If Approved → Execute Action (reduce rate_limit, suspend, etc.)
5. Log to governance_actions_log
```

### Governance Service

File: `pivota_infra/services/agent_governance_service.py` (new)

Functions:

- `propose_action(agent_id, action_type, reason)` - create pending action
- `execute_governance_action(action_id, executor_email)` - apply policy change
- `reject_governance_action(action_id, executor_email, reason)` - reject proposal
- `get_pending_actions()` - list actions awaiting approval

Action types:

- reduce_rate_limit (payload: new_limit)
- suspend_agent (payload: duration_hours)
- require_key_rotation (payload: deadline)
- data_quality_warning (payload: message)

### API Endpoints

New endpoints:

- `GET /admin/governance/pending-actions` - list pending governance actions
- `POST /admin/governance/actions/{action_id}/approve` - approve and execute
- `POST /admin/governance/actions/{action_id}/reject` - reject with reason
- `GET /agents/{id}/governance-history` - past actions for this agent

---

## Phase 3.4: Frontend Observability Dashboard

### New Components

**1. AgentMetricsChart.tsx** (new component)

- Line chart showing metrics over time (Recharts)
- Metrics: response time, error rate, qpm
- Time ranges: 1h, 6h, 24h, 7d
- Data polling: refresh every 30s

**2. AgentAlertsPanel.tsx** (new component)

- Table of unresolved alerts
- Severity badges (info/warning/critical)
- Resolve button
- Filter by severity, agent

**3. GovernanceActionsPanel.tsx** (new page or tab)

- Pending actions table
- Each row: agent, action type, reason, created time
- Approve/Reject buttons
- Action history log

### Extend AgentDetailPanel

Add new tab or collapsible section:

- Metrics History tab (chart + recent data points)
- Alerts tab (agent-specific alerts)
- Governance History (past actions on this agent)

### Update agents/page.tsx

Add top-level alerts banner if critical alerts exist:

```tsx
{criticalAlertsCount > 0 && (
  <AlertBanner>
    {criticalAlertsCount} agents have critical alerts requiring attention
  </AlertBanner>
)}
```

---

## Phase 3.5: Metrics Collection Integration

### Middleware for Automatic Collection

File: `pivota_infra/middleware/agent_telemetry.py` (new)

Intercept agent API calls:

- Record response time
- Record status code (success/error)
- Update agent.last_used_at
- Increment counters in Redis or temp storage
- Flush to DB every 5 minutes

OR simpler approach:

- Scheduled job queries agent_usage_logs table
- Aggregates last 5min of data
- Inserts into agent_metrics

### API Client Updates

File: `pivota-employee-portal-git/lib/api-client.ts`

Add methods:

- `getAgentMetricsHistory(agentId, hours)`
- `getAgentAlerts(agentId, resolved)`
- `resolveAlert(alertId)`
- `getPendingGovernanceActions()`
- `approveGovernanceAction(actionId)`
- `rejectGovernanceAction(actionId, reason)`

---

## Implementation Steps (Sub-phases)

### Step 1: Data Layer (Week 1)

- Create migration 009
- Implement metrics collector service
- Add scheduled job (every 5min)
- Test metrics collection

### Step 2: Detection & Alerts (Week 1-2)

- Implement anomaly detector
- Create alerts on anomalies
- Add alert CRUD endpoints
- Test alert generation

### Step 3: Governance Proposals (Week 2)

- Implement governance service
- Create pending actions on alerts
- Add approval/rejection endpoints
- Test governance flow

### Step 4: Frontend (Week 2-3)

- Build metrics chart component
- Build alerts panel
- Build governance actions panel
- Integrate with existing UI
- Add polling (30s refresh)

---

## Success Criteria

### Phase 3.1

- agent_metrics table populated every 5min
- Metrics API returns historical data
- Collection runs automatically

### Phase 3.2

- Alerts generated when anomalies detected
- Alert severity correctly assigned
- Employees can view and resolve alerts

### Phase 3.3

- Governance actions proposed when thresholds breached
- Approval workflow functional
- Actions logged in audit trail
- Rate limits/suspensions applied after approval

### Phase 3.4

- Frontend displays metrics charts
- Alerts visible and actionable
- Governance actions reviewable
- 30s polling works smoothly

---

## Out of Scope (Future Phase 4)

- WebSocket real-time push
- Redis Stream integration
- OpenTelemetry full integration
- ML-based anomaly detection
- Payment routing integration
- Multi-protocol layer optimization

## Notes

All Phase 3 features are additive - no changes to Phase 1/2 functionality.

Database-only approach keeps infrastructure simple and maintainable.

Human approval loop ensures safe governance in production.

### To-dos

- [ ] Create 008_agents_advanced_schema.sql migration with agent_api_keys, agent_protocols, agent_performance_stats tables
- [ ] Add API keys CRUD endpoints to employee_agent_mgmt.py
- [ ] Add protocols CRUD endpoints to employee_agent_mgmt.py
- [ ] Extend Agent interface in agents/page.tsx with api_keys and protocols arrays
- [ ] Add API Keys and Protocols sections to AgentDetailPanel component
- [ ] Test all new endpoints and UI functionality end-to-end