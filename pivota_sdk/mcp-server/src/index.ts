#!/usr/bin/env node

/**
 * Pivota MCP Server
 * Provides Model Context Protocol tools for AI assistants to interact with Pivota Agent API
 */

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  Tool,
} from '@modelcontextprotocol/sdk/types.js';
import axios, { AxiosInstance } from 'axios';

const API_KEY = process.env.PIVOTA_API_KEY;
const BASE_URL = process.env.PIVOTA_BASE_URL || 'https://web-production-fedb.up.railway.app/agent/v1';

if (!API_KEY) {
  console.error('Error: PIVOTA_API_KEY environment variable is required');
  process.exit(1);
}

// Create axios client
const apiClient: AxiosInstance = axios.create({
  baseURL: BASE_URL,
  headers: {
    'X-API-Key': API_KEY,
    'Content-Type': 'application/json',
  },
  timeout: 30000,
});

// Define available tools
const TOOLS: Tool[] = [
  {
    name: 'catalog_search',
    description: 'Search for products across all merchants. Returns a list of products matching the query.',
    inputSchema: {
      type: 'object',
      properties: {
        query: {
          type: 'string',
          description: 'Search query (e.g., "laptop", "coffee mug")',
        },
        merchant_id: {
          type: 'string',
          description: 'Optional: Filter by specific merchant ID',
        },
        min_price: {
          type: 'number',
          description: 'Optional: Minimum price filter',
        },
        max_price: {
          type: 'number',
          description: 'Optional: Maximum price filter',
        },
        limit: {
          type: 'number',
          description: 'Maximum number of results (default: 20)',
          default: 20,
        },
      },
      required: ['query'],
    },
  },
  {
    name: 'inventory_check',
    description: 'Check product inventory and availability',
    inputSchema: {
      type: 'object',
      properties: {
        product_id: {
          type: 'string',
          description: 'Product ID to check',
        },
        merchant_id: {
          type: 'string',
          description: 'Merchant ID',
        },
      },
      required: ['product_id', 'merchant_id'],
    },
  },
  {
    name: 'order_create',
    description: 'Create a new order. Returns order details including payment link.',
    inputSchema: {
      type: 'object',
      properties: {
        merchant_id: {
          type: 'string',
          description: 'Merchant ID',
        },
        items: {
          type: 'array',
          description: 'Array of items to order',
          items: {
            type: 'object',
            properties: {
              product_id: { type: 'string' },
              quantity: { type: 'number' },
              price: { type: 'number' },
            },
            required: ['product_id', 'quantity'],
          },
        },
        customer_email: {
          type: 'string',
          description: 'Customer email address',
        },
        shipping_address: {
          type: 'object',
          description: 'Shipping address (optional)',
          properties: {
            name: { type: 'string' },
            line1: { type: 'string' },
            city: { type: 'string' },
            state: { type: 'string' },
            postal_code: { type: 'string' },
            country: { type: 'string' },
          },
        },
      },
      required: ['merchant_id', 'items', 'customer_email'],
    },
  },
  {
    name: 'order_status',
    description: 'Get order status and tracking information',
    inputSchema: {
      type: 'object',
      properties: {
        order_id: {
          type: 'string',
          description: 'Order ID to check',
        },
      },
      required: ['order_id'],
    },
  },
  {
    name: 'list_merchants',
    description: 'List all available merchants',
    inputSchema: {
      type: 'object',
      properties: {
        status: {
          type: 'string',
          description: 'Filter by status (default: "active")',
          enum: ['active', 'inactive', 'pending'],
          default: 'active',
        },
        limit: {
          type: 'number',
          description: 'Maximum number of results (default: 50)',
          default: 50,
        },
      },
    },
  },
];

// Create and configure the server
const server = new Server(
  {
    name: 'pivota-mcp-server',
    version: '1.0.0',
  },
  {
    capabilities: {
      tools: {},
    },
  }
);

// List tools handler
server.setRequestHandler(ListToolsRequestSchema, async () => {
  return {
    tools: TOOLS,
  };
});

// Call tool handler
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  if (!args) {
    throw new Error('Arguments are required');
  }

  try {
    switch (name) {
      case 'catalog_search': {
        const response = await apiClient.get('/products/search', {
          params: {
            query: args.query,
            merchant_id: args.merchant_id,
            min_price: args.min_price,
            max_price: args.max_price,
            limit: args.limit || 20,
          },
        });
        
        const products = response.data.products || [];
        return {
          content: [
            {
              type: 'text',
              text: `Found ${products.length} products:\n\n` +
                products.map((p: any, i: number) => 
                  `${i + 1}. ${p.name}\n` +
                  `   Price: $${p.price}\n` +
                  `   ID: ${p.id}\n` +
                  `   Merchant: ${p.merchant_id || 'N/A'}`
                ).join('\n\n'),
            },
          ],
        };
      }

      case 'inventory_check': {
        const response = await apiClient.get(
          `/products/${args.merchant_id}/${args.product_id}`
        );
        
        const product = response.data;
        return {
          content: [
            {
              type: 'text',
              text: `Product: ${product.name}\n` +
                `Stock: ${product.stock_quantity || 'Unknown'}\n` +
                `Status: ${product.available ? 'Available' : 'Out of Stock'}\n` +
                `Price: $${product.price}`,
            },
          ],
        };
      }

      case 'order_create': {
        const response = await apiClient.post('/orders/create', {
          merchant_id: args.merchant_id,
          items: args.items,
          customer_email: args.customer_email,
          shipping_address: args.shipping_address,
        });
        
        const order = response.data;
        return {
          content: [
            {
              type: 'text',
              text: `✅ Order created successfully!\n\n` +
                `Order ID: ${order.order_id}\n` +
                `Status: ${order.status}\n` +
                `Total: $${order.total}\n` +
                `Payment Link: ${order.payment_url || 'N/A'}`,
            },
          ],
        };
      }

      case 'order_status': {
        const response = await apiClient.get(`/orders/${args.order_id}`);
        
        const order = response.data;
        return {
          content: [
            {
              type: 'text',
              text: `Order ID: ${order.order_id}\n` +
                `Status: ${order.status}\n` +
                `Payment Status: ${order.payment_status}\n` +
                `Total: $${order.total}\n` +
                `Created: ${new Date(order.created_at).toLocaleString()}`,
            },
          ],
        };
      }

      case 'list_merchants': {
        const response = await apiClient.get('/merchants', {
          params: {
            status: args.status || 'active',
            limit: args.limit || 50,
          },
        });
        
        const merchants = response.data.merchants || [];
        return {
          content: [
            {
              type: 'text',
              text: `Found ${merchants.length} merchants:\n\n` +
                merchants.map((m: any, i: number) =>
                  `${i + 1}. ${m.business_name}\n` +
                  `   ID: ${m.merchant_id}\n` +
                  `   Status: ${m.status}`
                ).join('\n\n'),
            },
          ],
        };
      }

      default:
        throw new Error(`Unknown tool: ${name}`);
    }
  } catch (error: any) {
    const errorMessage = error.response?.data?.detail || error.message || 'Unknown error';
    return {
      content: [
        {
          type: 'text',
          text: `Error: ${errorMessage}`,
        },
      ],
      isError: true,
    };
  }
});

// Start the server
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error('Pivota MCP Server running on stdio');
}

main().catch((error) => {
  console.error('Fatal error:', error);
  process.exit(1);
});

