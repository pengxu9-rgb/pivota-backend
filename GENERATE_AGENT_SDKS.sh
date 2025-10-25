#!/bin/bash

echo "🤖 Pivota Agent SDK Generator"
echo "========================================"
echo ""

# Configuration
API_URL="https://web-production-fedb.up.railway.app"
OPENAPI_SPEC_URL="$API_URL/openapi.json"
OUTPUT_DIR="$(pwd)/generated-sdks"

echo "📋 Configuration:"
echo "   API URL: $API_URL"
echo "   OpenAPI Spec: $OPENAPI_SPEC_URL"
echo "   Output Directory: $OUTPUT_DIR"
echo ""

# Create output directory
mkdir -p "$OUTPUT_DIR"

# Download OpenAPI spec and filter to agent endpoints only
echo "📥 Step 1: Downloading and filtering OpenAPI spec..."
python3 << 'FILTER_SPEC'
import urllib.request
import json
import sys

try:
    # Download full spec
    spec = json.loads(urllib.request.urlopen('https://web-production-fedb.up.railway.app/openapi.json').read())
    
    # Filter to only agent/v1 endpoints
    agent_paths = {k: v for k, v in spec.get('paths', {}).items() if '/agent/v1' in k}
    
    # Create filtered spec
    agent_spec = {
        "openapi": "3.0.0",
        "info": {
            "title": "Pivota Agent API",
            "version": "1.0.0",
            "description": "Production-ready API for AI agent integrations with e-commerce platforms",
            "contact": {
                "name": "Pivota Support",
                "email": "support@pivota.com",
                "url": "https://pivota.cc"
            }
        },
        "servers": [
            {
                "url": "https://web-production-fedb.up.railway.app/agent/v1",
                "description": "Production server"
            }
        ],
        "paths": agent_paths,
        "components": spec.get('components', {}),
        "security": [{"ApiKeyAuth": []}]
    }
    
    # Add security scheme if not present
    if 'securitySchemes' not in agent_spec['components']:
        agent_spec['components']['securitySchemes'] = {}
    
    agent_spec['components']['securitySchemes']['ApiKeyAuth'] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
        "description": "Agent API key (get from /agent/v1/auth endpoint)"
    }
    
    # Save to file
    with open('generated-sdks/agent-api-spec.json', 'w') as f:
        json.dump(agent_spec, f, indent=2)
    
    print(f"✅ Filtered spec saved: {len(agent_paths)} agent endpoints")
    
except Exception as e:
    print(f"❌ Error: {e}")
    sys.exit(1)

FILTER_SPEC

if [ $? -ne 0 ]; then
    echo "❌ Failed to download spec"
    exit 1
fi

echo ""

# Check if openapi-generator-cli is installed
echo "🔍 Step 2: Checking for openapi-generator-cli..."
if command -v openapi-generator-cli &> /dev/null; then
    echo "✅ openapi-generator-cli found"
elif command -v npx &> /dev/null; then
    echo "✅ Will use npx openapi-generator-cli"
    GENERATOR="npx --yes @openapitools/openapi-generator-cli"
else
    echo "⚠️  openapi-generator-cli not found"
    echo ""
    echo "📝 To install:"
    echo "   npm install -g @openapitools/openapi-generator-cli"
    echo "   # or use npx (no install needed)"
    echo ""
    echo "📝 Manual SDK generation:"
    echo "   npx @openapitools/openapi-generator-cli generate \\"
    echo "     -i generated-sdks/agent-api-spec.json \\"
    echo "     -g python \\"
    echo "     -o generated-sdks/python-sdk \\"
    echo "     --additional-properties=packageName=pivota_agent"
    echo ""
    exit 0
fi

echo ""

# Generate Python SDK
echo "🐍 Step 3: Generating Python SDK..."
npx --yes @openapitools/openapi-generator-cli generate \
  -i "$OUTPUT_DIR/agent-api-spec.json" \
  -g python \
  -o "$OUTPUT_DIR/python-sdk" \
  --additional-properties=packageName=pivota_agent,projectName=pivota-agent-sdk,packageVersion=1.0.0

if [ $? -eq 0 ]; then
    echo "✅ Python SDK generated at: $OUTPUT_DIR/python-sdk"
else
    echo "❌ Python SDK generation failed"
fi

echo ""

# Generate TypeScript SDK
echo "📘 Step 4: Generating TypeScript SDK..."
npx --yes @openapitools/openapi-generator-cli generate \
  -i "$OUTPUT_DIR/agent-api-spec.json" \
  -g typescript-axios \
  -o "$OUTPUT_DIR/typescript-sdk" \
  --additional-properties=npmName=@pivota/agent-sdk,npmVersion=1.0.0,supportsES6=true

if [ $? -eq 0 ]; then
    echo "✅ TypeScript SDK generated at: $OUTPUT_DIR/typescript-sdk"
else
    echo "❌ TypeScript SDK generation failed"
fi

echo ""
echo "========================================"
echo "✅ SDK Generation Complete!"
echo ""
echo "📦 Generated SDKs:"
echo "   • Python: $OUTPUT_DIR/python-sdk"
echo "   • TypeScript: $OUTPUT_DIR/typescript-sdk"
echo ""
echo "📝 Next steps:"
echo "   1. Test the SDKs"
echo "   2. Publish to PyPI (Python) and npm (TypeScript)"
echo "   3. Create usage documentation"





