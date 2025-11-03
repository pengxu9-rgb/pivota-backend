# 🚀 Payment Infrastructure Dashboard - Deployment Guide

## 📋 **Deployment Options**

### 1. **Railway (Recommended - Easiest)**
```bash
# Install Railway CLI
npm install -g @railway/cli

# Login to Railway
railway login

# Deploy from your project directory
railway up
```

### 2. **Render (Free Tier Available)**
```bash
# Connect your GitHub repo to Render
# Go to: https://render.com
# Create new Web Service
# Connect GitHub repository
# Use these settings:
```

**Render Configuration:**
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
- **Environment:** Python 3.9+

### 3. **Heroku**
```bash
# Install Heroku CLI
# Login to Heroku
heroku login

# Create Heroku app
heroku create your-pivota-dashboard

# Set environment variables
heroku config:set STRIPE_SECRET_KEY=your_stripe_key
heroku config:set ADYEN_API_KEY=your_adyen_key
heroku config:set ADYEN_MERCHANT_ACCOUNT=your_merchant_account

# Deploy
git push heroku main
```

### 4. **DigitalOcean App Platform**
```bash
# Create app.yaml in your project root
# Deploy via DigitalOcean dashboard
```

### 5. **AWS/GCP/Azure (Production)**
```bash
# Use Docker containers
# Deploy to Kubernetes or ECS
# Set up load balancers and databases
```

## 🔧 **Environment Variables**

Create a `.env` file with your production credentials:

```env
# Database
DATABASE_URL=postgresql://user:password@host:port/database

# JWT Secret (Generate a strong secret)
JWT_SECRET_KEY=your-super-secret-jwt-key-here

# Stripe
STRIPE_SECRET_KEY=sk_live_your_stripe_key
STRIPE_PUBLISHABLE_KEY=pk_live_your_stripe_key
STRIPE_WEBHOOK_SECRET=whsec_your_webhook_secret

# Adyen
ADYEN_API_KEY=your_adyen_api_key
ADYEN_MERCHANT_ACCOUNT=your_merchant_account
ADYEN_WEBHOOK_SECRET=your_adyen_webhook_secret

# PayPal
PAYPAL_CLIENT_ID=your_paypal_client_id
PAYPAL_CLIENT_SECRET=your_paypal_client_secret
PAYPAL_ENVIRONMENT=live

# CORS (for production)
ALLOWED_ORIGINS=https://yourdomain.com,https://yourdashboard.com
```

## 🐳 **Docker Deployment**

### Dockerfile
```dockerfile
FROM python:3.9-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port
EXPOSE 8000

# Run the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### docker-compose.yml
```yaml
version: '3.8'

services:
  pivota-dashboard:
    build: .
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=postgresql://user:password@db:5432/pivota
      - JWT_SECRET_KEY=your-jwt-secret
    depends_on:
      - db
      - redis

  db:
    image: postgres:13
    environment:
      POSTGRES_DB: pivota
      POSTGRES_USER: user
      POSTGRES_PASSWORD: password
    volumes:
      - postgres_data:/var/lib/postgresql/data

  redis:
    image: redis:6-alpine
    ports:
      - "6379:6379"

volumes:
  postgres_data:
```

## 🚀 **Quick Deploy Commands**

### Railway (Fastest)
```bash
# 1. Install Railway CLI
npm install -g @railway/cli

# 2. Login
railway login

# 3. Deploy
railway up
```

### Render (Free)
```bash
# 1. Push to GitHub
git add .
git commit -m "Deploy Payment Dashboard"
git push origin main

# 2. Go to render.com
# 3. Connect GitHub repo
# 4. Deploy automatically
```

### Heroku
```bash
# 1. Create Procfile
echo "web: uvicorn main:app --host 0.0.0.0 --port \$PORT" > Procfile

# 2. Deploy
git add .
git commit -m "Deploy to Heroku"
git push heroku main
```

## 🔐 **Security Checklist**

- [ ] Change default JWT secret
- [ ] Use production API keys
- [ ] Enable HTTPS
- [ ] Set up CORS properly
- [ ] Configure rate limiting
- [ ] Set up monitoring
- [ ] Enable logging
- [ ] Backup database

## 📊 **Monitoring & Analytics**

### Health Check Endpoints
- `GET /health` - Basic health check
- `GET /api/dashboard/status` - Dashboard status
- `GET /api/payments/status` - Payment system status

### Metrics
- Order processing rates
- Payment success rates
- PSP performance
- User activity
- Error rates

## 🌐 **Domain & SSL**

### Custom Domain Setup
1. **Buy domain** (e.g., pivota-dashboard.com)
2. **Configure DNS** to point to your deployment
3. **Enable SSL** (automatic with most platforms)
4. **Update CORS** settings

### Production URLs
- **Dashboard:** https://yourdomain.com
- **API Docs:** https://yourdomain.com/docs
- **Health Check:** https://yourdomain.com/health

## 🔄 **CI/CD Pipeline**

### GitHub Actions Example
```yaml
name: Deploy to Production

on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - name: Deploy to Railway
        run: railway up
        env:
          RAILWAY_TOKEN: ${{ secrets.RAILWAY_TOKEN }}
```

## 📱 **Frontend Integration**

### Lovable Dashboard
Your Lovable dashboard can connect to the deployed API:

```javascript
// Update your Lovable dashboard
const API_BASE_URL = 'https://your-deployed-domain.com';

// WebSocket connection
const ws = new WebSocket('wss://your-deployed-domain.com/ws/metrics');

// API calls
fetch(`${API_BASE_URL}/api/dashboard/analytics`, {
  headers: {
    'Authorization': `Bearer ${token}`
  }
});
```

## 🎯 **Next Steps After Deployment**

1. **Test all endpoints** with your production credentials
2. **Set up monitoring** (Sentry, DataDog, etc.)
3. **Configure webhooks** for PSPs
4. **Set up database backups**
5. **Configure logging** and alerting
6. **Update your Lovable dashboard** with production URLs
7. **Test with real payments** (small amounts first!)

## 🆘 **Troubleshooting**

### Common Issues
- **CORS errors:** Update ALLOWED_ORIGINS
- **Database connection:** Check DATABASE_URL
- **JWT errors:** Verify JWT_SECRET_KEY
- **PSP errors:** Check API keys and webhooks

### Debug Commands
```bash
# Check logs
railway logs

# Check environment
railway variables

# Restart service
railway redeploy
```

---

## 🚀 **Ready to Deploy!**

Choose your preferred platform and follow the steps above. The **Railway** option is the fastest for getting started, while **Render** offers a generous free tier.
