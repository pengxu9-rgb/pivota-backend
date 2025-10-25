#!/bin/bash

# Pivota SDK Publishing Script
# Run this in your local terminal (not in Cursor)

set -e

echo "🚀 Pivota SDK Publishing Script"
echo "================================"
echo ""

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to publish Python SDK
publish_python() {
    echo -e "${BLUE}📦 Publishing Python SDK to PyPI...${NC}"
    cd pivota_sdk/python
    
    # Check if twine is installed
    if ! command -v twine &> /dev/null; then
        echo "Installing twine..."
        pip install twine
    fi
    
    echo "Publishing to PyPI..."
    echo "You'll be prompted for your PyPI credentials:"
    echo "  Username: __token__"
    echo "  Password: (paste your PyPI API token)"
    
    python -m twine upload dist/*
    
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✅ Python SDK published successfully!${NC}"
        echo "Test with: pip install pivota-agent"
    else
        echo -e "${YELLOW}⚠️  Python SDK publishing failed${NC}"
    fi
    
    cd ../..
}

# Function to publish TypeScript SDK
publish_typescript() {
    echo -e "${BLUE}📦 Publishing TypeScript SDK to npm...${NC}"
    cd pivota_sdk/typescript
    
    echo "Make sure you're logged in to npm:"
    echo "Run: npm login"
    echo ""
    read -p "Press Enter when ready to publish..."
    
    npm publish --access public
    
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✅ TypeScript SDK published successfully!${NC}"
        echo "Test with: npm install pivota-agent"
    else
        echo -e "${YELLOW}⚠️  TypeScript SDK publishing failed${NC}"
    fi
    
    cd ../..
}

# Function to publish MCP Server
publish_mcp() {
    echo -e "${BLUE}📦 Publishing MCP Server to npm...${NC}"
    cd pivota_sdk/mcp-server
    
    echo "Make sure you're logged in to npm:"
    echo "Run: npm login"
    echo ""
    read -p "Press Enter when ready to publish..."
    
    npm publish --access public
    
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✅ MCP Server published successfully!${NC}"
        echo "Test with: npx pivota-mcp-server"
    else
        echo -e "${YELLOW}⚠️  MCP Server publishing failed${NC}"
    fi
    
    cd ../..
}

# Main menu
echo "Select what to publish:"
echo "1) Python SDK only"
echo "2) TypeScript SDK only"
echo "3) MCP Server only"
echo "4) All three packages"
echo "5) Exit"
echo ""
read -p "Enter choice [1-5]: " choice

case $choice in
    1)
        publish_python
        ;;
    2)
        publish_typescript
        ;;
    3)
        publish_mcp
        ;;
    4)
        publish_python
        echo ""
        publish_typescript
        echo ""
        publish_mcp
        ;;
    5)
        echo "Exiting..."
        exit 0
        ;;
    *)
        echo "Invalid choice"
        exit 1
        ;;
esac

echo ""
echo -e "${GREEN}✅ Publishing complete!${NC}"
echo ""
echo "Next steps:"
echo "1. Test installations:"
echo "   pip install pivota-agent"
echo "   npm install pivota-agent"
echo "   npx pivota-mcp-server --help"
echo ""
echo "2. Verify on package registries:"
echo "   https://pypi.org/project/pivota-agent/"
echo "   https://www.npmjs.com/package/pivota-agent"
echo "   https://www.npmjs.com/package/pivota-mcp-server"


