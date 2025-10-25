# Pivota MCP Server

Model Context Protocol (MCP) server for Pivota Agent API. Enables AI assistants like Claude to search products, create orders, and manage e-commerce workflows.

## Installation

```bash
npx pivota-mcp-server
```

Or install globally:

```bash
npm install -g pivota-mcp-server
```

## Usage

### Claude Desktop Configuration

Add to your `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pivota": {
      "command": "npx",
      "args": ["-y", "pivota-mcp-server"],
      "env": {
        "PIVOTA_API_KEY": "your_api_key_here"
      }
    }
  }
}
```

### Environment Variables

- `PIVOTA_API_KEY` (required): Your Pivota Agent API key
- `PIVOTA_BASE_URL` (optional): API base URL (default: https://web-production-fedb.up.railway.app/agent/v1)

## Available Tools

### catalog_search
Search for products across all merchants.

**Parameters:**
- `query` (required): Search query
- `merchant_id` (optional): Filter by merchant
- `min_price` (optional): Minimum price
- `max_price` (optional): Maximum price
- `limit` (optional): Max results (default: 20)

**Example:**
```
User: "Find me a laptop under $1000"
Claude will use: catalog_search(query="laptop", max_price=1000)
```

### inventory_check
Check product availability and stock.

**Parameters:**
- `product_id` (required): Product ID
- `merchant_id` (required): Merchant ID

### order_create
Create a new order.

**Parameters:**
- `merchant_id` (required): Merchant ID
- `items` (required): Array of items with product_id, quantity, price
- `customer_email` (required): Customer email
- `shipping_address` (optional): Shipping details

**Example:**
```
User: "Order that laptop for me"
Claude will use: order_create(merchant_id="...", items=[...], customer_email="...")
```

### order_status
Get order status and tracking.

**Parameters:**
- `order_id` (required): Order ID

### list_merchants
List all available merchants.

**Parameters:**
- `status` (optional): Filter by status (default: "active")
- `limit` (optional): Max results (default: 50)

## Example Conversation

```
You: Find me a coffee mug under $20

Claude: I'll search for coffee mugs in that price range...
        [Uses catalog_search tool]
        I found 5 options. The 'Ceramic Coffee Mug' from ChydanTest Store 
        is $15.99. Would you like me to create an order?

You: Yes, please order it

Claude: [Uses order_create tool]
        ✅ Order created successfully! Order ID: ord_abc123
        Total: $15.99. You'll receive a confirmation email shortly.
```

## Development

```bash
# Install dependencies
npm install

# Build
npm run build

# Run locally
npm start
```

## License

MIT

## Support

- Documentation: https://agents.pivota.cc/developers/docs
- API Reference: https://web-production-fedb.up.railway.app/agent/v1/openapi.json
- GitHub: https://github.com/pivota/pivota-mcp-server



