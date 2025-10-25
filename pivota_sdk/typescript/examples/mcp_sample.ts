import { PivotaAgentClient } from '../src/client';

async function main() {
  const apiKey = process.env.PIVOTA_AGENT_API_KEY || 'ak_live_a9357ba726397c8e5e636ca2d197b8105cec414a3ea2c29b453f275af5cf3ca3';
  const client = new PivotaAgentClient({ apiKey, baseUrl: 'https://web-production-fedb.up.railway.app/agent/v1' });

  console.log('Health check...');
  const health = await client.healthCheck();
  console.log('Health:', health);

  console.log('\nListing merchants...');
  const merchants = await client.listMerchants({ status: 'approved', limit: 5 });
  console.log('Merchants:', merchants.map(m => ({ id: (m as any).merchant_id || (m as any).id, name: (m as any).business_name || (m as any).name })));

  console.log('\nSearching products (coffee)...');
  const search = await client.searchProducts({ query: 'coffee', limit: 1 } as any);
  const product = (search as any).products?.[0];
  if (!product) {
    console.error('No products found');
    return;
  }
  const merchantId = (product as any).merchant_id || (merchants[0] && ((merchants[0] as any).merchant_id || (merchants[0] as any).id));
  console.log('Product:', { id: product.id, name: product.name, price: product.price, merchantId });

  console.log('\nCreating order...');
  const order = await client.createOrder({
    merchant_id: merchantId,
    customer_email: 'john.doe@example.com',
    items: [
      {
        product_id: product.id,
        product_title: product.name,
        quantity: 2,
        unit_price: Number(product.price) || 0,
        subtotal: (Number(product.price) || 0) * 2,
      },
    ],
    shipping_address: {
      name: 'John Doe',
      address_line1: '123 Main St',
      city: 'San Francisco',
      state: 'CA',
      postal_code: '94105',
      country: 'US',
    },
  } as any);
  console.log('Order created:', { order_id: (order as any).order_id, status: (order as any).status, total: (order as any).total });
}

main().catch(err => {
  console.error('Error:', (err as any)?.response?.data || err);
  process.exit(1);
});






