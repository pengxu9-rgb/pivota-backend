#!/bin/bash

# Quick test script to verify backend is working

echo "🔍 Testing Pivota Backend..."
echo ""

echo "1️⃣ Testing root endpoint..."
response=$(curl -s -w "\nTime: %{time_total}s\n" https://pivota-dashboard.onrender.com/)
echo "$response"
echo ""

echo "2️⃣ Testing signin endpoint..."
response=$(curl -s -w "\nTime: %{time_total}s\n" -X POST https://pivota-dashboard.onrender.com/auth/signin \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@pivota.com","password":"admin123"}')
echo "$response"
echo ""

echo "3️⃣ Testing with wrong credentials (should fail)..."
response=$(curl -s -w "\nTime: %{time_total}s\n" -X POST https://pivota-dashboard.onrender.com/auth/signin \
  -H "Content-Type: application/json" \
  -d '{"email":"wrong@test.com","password":"wrong"}')
echo "$response"
echo ""

echo "✅ Backend tests complete!"
echo ""
echo "If all responses came back in < 5 seconds, backend is healthy."
echo "Now test the frontend at: http://localhost:3000/"

