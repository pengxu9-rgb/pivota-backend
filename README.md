# 🚀 Payment Infrastructure Dashboard

A comprehensive payment infrastructure system with multi-role authentication, PSP integration, and real-time analytics.

## ✨ Features

- **Multi-Role Authentication** (Admin, Merchant, Agent)
- **PSP Integration** (Stripe, Adyen, PayPal)
- **Order Orchestration** with retry logic
- **Real-time Analytics** dashboard
- **WebSocket** real-time updates
- **Production Ready** with environment variables

## 🚀 Quick Start

### Environment Variables

Create a `.env` file with your credentials:

```env
# JWT Authentication
JWT_SECRET_KEY=your-jwt-secret-key-here

# Stripe Production
STRIPE_SECRET_KEY=sk_live_your_stripe_secret_key_here
STRIPE_PUBLISHABLE_KEY=pk_live_your_stripe_publishable_key_here

# Adyen Production
ADYEN_API_KEY=your_adyen_api_key_here
ADYEN_MERCHANT_ACCOUNT=your_merchant_account_here

# Database
DATABASE_URL=postgresql://user:password@host:port/database
```

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run the application
uvicorn main:app --host 0.0.0.0 --port 8000
```

### Production Deployment

#### Render
1. Connect your GitHub repository
2. Set environment variables
3. Deploy automatically

#### Railway
```bash
npm install -g @railway/cli
railway login
railway up
```

#### Docker
```bash
docker-compose up -d
```

## 📊 API Endpoints

- `GET /` - Health check
- `GET /docs` - API documentation
- `POST /api/dashboard/auth/login` - Authentication
- `GET /api/dashboard/analytics` - Analytics data
- `POST /api/payments/process` - Process payments
- `WebSocket /ws/metrics` - Real-time metrics

## 🔐 Authentication

### Demo Users
- **Admin:** username: `admin`, password: `admin123`
- **Merchant:** username: `shopify_store`, password: `merchant123`
- **Agent:** username: `agent_ai`, password: `agent123`

## 🎯 Production Ready

This system is configured with:
- ✅ Real Stripe production keys
- ✅ Real Adyen production keys
- ✅ Multi-role authentication
- ✅ PSP routing logic
- ✅ Order orchestration
- ✅ Real-time analytics
- ✅ WebSocket updates

## 📈 Dashboard Features

- **Real-time Metrics** - Live payment processing stats
- **PSP Performance** - Success rates and latency tracking
- **Order Management** - Track orders across merchants
- **Analytics** - Comprehensive reporting
- **Multi-role Views** - Different dashboards for Admin/Merchant/Agent

## 🚀 Deploy Now

Your Payment Infrastructure Dashboard is ready for production deployment!

Choose your preferred platform:
- **Render** (Easiest - No CLI needed)
- **Railway** (Fast deployment)
- **Heroku** (Classic)
- **Docker** (Any platform)