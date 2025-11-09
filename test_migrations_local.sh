#!/bin/bash
# Local Migration Testing Script
# Tests all database migrations in a clean Docker Postgres environment

set -e

CONTAINER_NAME="pivota-test-postgres"
DB_NAME="pivota_test"
DB_USER="postgres"
DB_PASSWORD="test_password"

echo "🧪 Testing Database Migrations Locally"
echo "========================================"
echo ""

# 1. Clean up any existing container
echo "1️⃣ Cleaning up existing test container..."
docker stop $CONTAINER_NAME 2>/dev/null || true
docker rm $CONTAINER_NAME 2>/dev/null || true
echo "✅ Cleanup complete"
echo ""

# 2. Start fresh Postgres container
echo "2️⃣ Starting fresh Postgres container..."
docker run --name $CONTAINER_NAME \
  -e POSTGRES_PASSWORD=$DB_PASSWORD \
  -e POSTGRES_DB=$DB_NAME \
  -d postgres:15

echo "⏳ Waiting for Postgres to be ready..."
sleep 5

# Test connection
docker exec $CONTAINER_NAME pg_isready -U $DB_USER
echo "✅ Postgres is ready"
echo ""

# 3. Apply migrations sequentially
echo "3️⃣ Applying migrations..."
MIGRATION_DIR="pivota_infra/db/migrations"
FAILED=0

for migration_file in $(ls $MIGRATION_DIR/*.sql | sort); do
    filename=$(basename $migration_file)
    echo ""
    echo "📄 Testing: $filename"
    echo "   Path: $migration_file"
    
    # Apply migration
    if docker exec -i $CONTAINER_NAME psql -U $DB_USER -d $DB_NAME < $migration_file 2>&1 | tee /tmp/migration_output.log; then
        echo "   ✅ SUCCESS"
    else
        echo "   ❌ FAILED"
        echo ""
        echo "Error output:"
        cat /tmp/migration_output.log
        FAILED=1
        break
    fi
done

echo ""
echo "========================================"

# 4. Cleanup
echo "4️⃣ Cleaning up..."
docker stop $CONTAINER_NAME
docker rm $CONTAINER_NAME
echo "✅ Cleanup complete"
echo ""

# 5. Report results
if [ $FAILED -eq 0 ]; then
    echo "✅ All migrations passed!"
    echo ""
    echo "Summary:"
    echo "  - Container: $CONTAINER_NAME"
    echo "  - Database: $DB_NAME"
    echo "  - Migrations tested: $(ls $MIGRATION_DIR/*.sql | wc -l)"
    exit 0
else
    echo "❌ Migration testing failed!"
    echo ""
    echo "Please fix the failing migration and try again."
    exit 1
fi

