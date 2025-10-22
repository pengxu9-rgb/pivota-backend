"""Fix orders table columns"""
from fastapi import APIRouter, HTTPException
from db.database import database
from utils.logger import logger
from sqlalchemy import text

router = APIRouter(prefix="/admin/fix", tags=["admin"])

@router.post("/orders-table-columns")
async def fix_orders_table_columns():
    """Add missing columns to orders table"""
    try:
        # Add missing columns one by one
        fixes_applied = []
        
        # shipping_address column
        try:
            await database.execute(text("""
                ALTER TABLE orders 
                ADD COLUMN shipping_address JSONB
            """))
            fixes_applied.append("Added shipping_address column")
        except Exception as e:
            if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
                fixes_applied.append("shipping_address column already exists")
            else:
                logger.warning(f"Could not add shipping_address: {e}")
        
        # items column
        try:
            await database.execute(text("""
                ALTER TABLE orders 
                ADD COLUMN items JSONB
            """))
            fixes_applied.append("Added items column")
        except Exception as e:
            if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
                fixes_applied.append("items column already exists")
            else:
                logger.warning(f"Could not add items: {e}")
        
        # client_secret column
        try:
            await database.execute(text("""
                ALTER TABLE orders 
                ADD COLUMN client_secret VARCHAR(500)
            """))
            fixes_applied.append("Added client_secret column")
        except Exception as e:
            if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
                fixes_applied.append("client_secret column already exists")
            else:
                logger.warning(f"Could not add client_secret: {e}")
        
        # subtotal column
        try:
            await database.execute(text("""
                ALTER TABLE orders 
                ADD COLUMN subtotal NUMERIC(10,2)
            """))
            fixes_applied.append("Added subtotal column")
        except Exception as e:
            if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
                fixes_applied.append("subtotal column already exists")
            else:
                logger.warning(f"Could not add subtotal: {e}")
        
        # tax column
        try:
            await database.execute(text("""
                ALTER TABLE orders 
                ADD COLUMN tax NUMERIC(10,2)
            """))
            fixes_applied.append("Added tax column")
        except Exception as e:
            if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
                fixes_applied.append("tax column already exists")
            else:
                logger.warning(f"Could not add tax: {e}")
        
        # total column
        try:
            await database.execute(text("""
                ALTER TABLE orders 
                ADD COLUMN total NUMERIC(10,2)
            """))
            fixes_applied.append("Added total column")
        except Exception as e:
            if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
                fixes_applied.append("total column already exists")
            else:
                logger.warning(f"Could not add total: {e}")
        
        # payment_status column
        try:
            await database.execute(text("""
                ALTER TABLE orders 
                ADD COLUMN payment_status VARCHAR(50)
            """))
            fixes_applied.append("Added payment_status column")
        except Exception as e:
            if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
                fixes_applied.append("payment_status column already exists")
            else:
                logger.warning(f"Could not add payment_status: {e}")
        
        # agent_id column
        try:
            await database.execute(text("""
                ALTER TABLE orders 
                ADD COLUMN agent_id VARCHAR(50)
            """))
            fixes_applied.append("Added agent_id column")
        except Exception as e:
            if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
                fixes_applied.append("agent_id column already exists")
            else:
                logger.warning(f"Could not add agent_id: {e}")
        
        # Verify columns exist
        columns = await database.fetch_all(text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'orders'
            ORDER BY ordinal_position
        """))
        
        column_names = [col["column_name"] for col in columns]
        
        return {
            "status": "success",
            "fixes_applied": fixes_applied,
            "current_columns": column_names,
            "has_shipping_address": "shipping_address" in column_names,
            "has_items": "items" in column_names,
            "has_client_secret": "client_secret" in column_names,
            "has_subtotal": "subtotal" in column_names,
            "has_tax": "tax" in column_names,
            "has_total": "total" in column_names,
            "has_payment_status": "payment_status" in column_names,
            "has_agent_id": "agent_id" in column_names
        }
        
    except Exception as e:
        logger.error(f"Failed to fix orders table: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/orders-table-info")
async def get_orders_table_info():
    """Get current orders table structure"""
    try:
        columns = await database.fetch_all(text("""
            SELECT column_name, data_type 
            FROM information_schema.columns 
            WHERE table_name = 'orders'
            ORDER BY ordinal_position
        """))
        
        return {
            "status": "success",
            "columns": [
                {"name": col["column_name"], "type": col["data_type"]} 
                for col in columns
            ]
        }
        
    except Exception as e:
        logger.error(f"Failed to get orders table info: {e}")
        raise HTTPException(status_code=500, detail=str(e))
