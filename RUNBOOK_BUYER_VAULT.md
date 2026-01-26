# RUNBOOK — Buyer Vault / Unified Buyer Account（MVP）

目标：演示 “agent mint intent → checkout 预填 → Save for next time（step-up）→ 下次任意 agent 自动预填”。

---

## 0) 环境变量（后端）

必须：
- `CHECKOUT_TOKEN_SECRET`：签发/校验 `X-Checkout-Token`
- `CHECKOUT_UI_KEY`：Checkout UI 后端专用 secret（仅 server-side；不要出现在浏览器 bundle/日志里）

可选：
- `DATABASE_URL`：不配置则默认使用 `sqlite+aiosqlite:///./pivota.db`
- `CHECKOUT_UI_BASE_URL`：用于拼接 step-up `login_url`（默认 `https://agent.pivota.cc`）
- `CHECKOUT_INTENT_TTL_SECONDS`：intent TTL（默认 1800）
- `CHECKOUT_TOKEN_TTL_SECONDS`：checkout token TTL（默认同 intent TTL）
- `CHECKOUT_PREFILL_MAX_READS`：prefill 最大读取次数（默认 3）
- `CHECKOUT_PREFILL_INCLUDE_PHONE`：prefill 是否返回 phone（默认 false）
- `BUYER_SAVE_TOKEN_TTL_SECONDS`：save_token TTL（默认 900）

---

## 1) 启动后端

在 `pivota-backend/`：

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export CHECKOUT_TOKEN_SECRET='dev_secret'
export CHECKOUT_UI_KEY='dev_ui_key'

# 可选：本地 SQLite（不配则默认 pivota-backend/pivota.db）
export DATABASE_URL='sqlite+aiosqlite:////tmp/pivota_buyer_vault_demo.db'

uvicorn main:app --reload --port 8000
```

### 1.1) Seed 一个本地 agent（拿到 X-API-Key）

在同一个终端/另一个终端（确保 `DATABASE_URL` 一致）：

```bash
python3 scripts/dev_seed_agents.py --count 1
```

输出里的 `agents[0].api_key` 用于下面的 `X-API-Key`。

---

## 2) Demo Flow

### 2.1 Agent mint intent（拿到 checkout_url / checkout_token）

```bash
curl -sS http://localhost:8000/agent/v1/checkout/intents \
  -H 'X-API-Key: <your_agent_api_key>' \
  -H 'Content-Type: application/json' \
  -d '{
    "items":[{"product_id":"prod_1","variant_id":"var_1","merchant_id":"merch_xxx","quantity":1}],
    "customer_email":"buyer@example.com",
    "shipping_address":{"name":"Jane Doe","address_line1":"1 Market St","city":"SF","state":"CA","postal_code":"94105","country":"US"}
  }'
```

### 2.2 直接验收 prefill（不跑前端也可）

```bash
curl -sS http://localhost:8000/agent/v1/checkout/prefill \
  -H 'X-Checkout-Token: <checkout_token>' \
  -H 'X-Checkout-UI-Key: dev_ui_key'
```

---

## 3) 测试

```bash
python3 -m pytest -q
```

