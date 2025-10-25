# @pivota/agent-sdk

Official TypeScript/JavaScript SDK for integrating AI agents with the Pivota e-commerce platform.

## 🚀 Installation

```bash
npm install @pivota/agent-sdk
# or
yarn add @pivota/agent-sdk
```

## Quick Start

```typescript
import { PivotaAgentClient } from '@pivota/agent-sdk';

// Create agent and get API key
const client = await PivotaAgentClient.createAgent(
  'MyShoppingBot',
  'bot@mycompany.com',
  'AI shopping assistant'
);

console.log(`API Key: ${client.apiKey}`);
// Save this key securely!

// Or initialize with existing key
const client = new PivotaAgentClient({
  apiKey: 'ak_live_your_key_here'
});
```

## 📚 Examples

### Search Products

```typescript
// Search across all merchants
const result = await client.searchProducts({
  query: 'gaming laptop',
  maxPrice: 1500,
  limit: 10
});

result.products.forEach(product => {
  console.log(`${product.name} - $${product.price} - ${product.merchant_name}`);
});
```

### Create Order

```typescript
const order = await client.createOrder({
  merchant_id: 'merch_xxx',
  items: [
    { product_id: 'prod_123', quantity: 1 },
    { product_id: 'prod_456', quantity: 2 }
  ],
  customer_email: 'buyer@example.com',
  shipping_address: {
    street: '123 Main St',
    city: 'San Francisco',
    state: 'CA',
    zip: '94105',
    country: 'US'
  }
});

console.log(`Order created: ${order.order_id}`);
console.log(`Total: $${order.total_amount}`);
```

### Process Payment

```typescript
const payment = await client.createPayment({
  order_id: order.order_id,
  payment_method: {
    type: 'card',
    token: 'tok_visa_test' // From Stripe/Adyen
  },
  return_url: 'https://mybot.com/payment-callback'
});

if (payment.status === 'requires_action') {
  // Handle 3DS
  console.log(`Redirect to: ${payment.next_action?.redirect_url}`);
} else if (payment.status === 'succeeded') {
  console.log('Payment successful!');
}
```

## 🔧 Error Handling

```typescript
import {
  AuthenticationError,
  RateLimitError,
  NotFoundError,
  ValidationError
} from '@pivota/agent-sdk';

try {
  const products = await client.searchProducts({ query: 'laptop' });
} catch (error) {
  if (error instanceof AuthenticationError) {
    console.error('Invalid API key');
  } else if (error instanceof RateLimitError) {
    console.error(`Rate limited. Retry after ${error.retryAfter}s`);
  } else if (error instanceof NotFoundError) {
    console.error('Resource not found');
  } else {
    console.error(`API error: ${error.message}`);
  }
}
```

## 📖 API Reference

See [full documentation](https://docs.pivota.com/agent-sdk)

## 🔒 Rate Limits

- Standard tier: 1,000 requests/minute
- Burst: Up to 50 requests in first 10 seconds

## 🆘 Support

- Docs: https://docs.pivota.com
- GitHub: https://github.com/pivota/pivota-agent-sdk-typescript
- Email: support@pivota.com

## 📄 License

MIT






