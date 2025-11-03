#!/bin/bash

echo "Checking Agent Portal API endpoints..."
echo "======================================"

BASE_URL="https://web-production-fedb.up.railway.app"

# List of endpoints to check
endpoints=(
    "GET:/agent/account/login"
    "POST:/agent/account/login"
    "GET:/agent/metrics/summary"
    "GET:/agent/metrics/recent"
    "GET:/agent/v1/metrics/timeline"
    "GET:/agent/health"
    "GET:/agent/v1/orders"
    "GET:/agents/{id}"
    "GET:/agents/{id}/merchants"
    "GET:/agents/{id}/funnel"
    "GET:/agents/{id}/query-analytics"
    "GET:/agents/{id}/api-keys"
    "POST:/agents/{id}/api-keys"
    "DELETE:/agents/{id}/api-keys/{keyId}"
    "PUT:/agents/{id}"
    "POST:/agents/{id}/reset-api-key"
    "POST:/agent/v1/orders/{id}/refund"
    "POST:/agent/v1/orders/{id}/cancel"
    "GET:/agent/v1/orders/{id}/track"
)

for endpoint in "${endpoints[@]}"; do
    method="${endpoint%%:*}"
    path="${endpoint#*:}"
    
    # Replace {id} with test values
    test_path="${path//\{id\}/test123}"
    test_path="${test_path//\{keyId\}/key123}"
    
    echo -n "Testing $method $path... "
    
    if [ "$method" = "GET" ]; then
        status=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL$test_path")
    else
        status=$(curl -s -o /dev/null -w "%{http_code}" -X "$method" "$BASE_URL$test_path")
    fi
    
    if [ "$status" = "404" ]; then
        echo "❌ NOT FOUND"
    elif [ "$status" = "401" ] || [ "$status" = "403" ]; then
        echo "✅ EXISTS (requires auth)"
    elif [ "$status" = "405" ]; then
        echo "⚠️  Method not allowed"
    elif [ "$status" = "422" ] || [ "$status" = "400" ]; then
        echo "✅ EXISTS (bad request)"
    elif [ "$status" = "500" ]; then
        echo "⚠️  SERVER ERROR"
    else
        echo "✅ EXISTS (status: $status)"
    fi
done
