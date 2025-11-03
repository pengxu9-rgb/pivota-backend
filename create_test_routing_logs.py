#!/usr/bin/env python3
"""
[Phase 4++] 创建测试路由日志数据
"""

import asyncio
import sys
import os
from datetime import datetime, timedelta
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pivota_infra.db.database import database

async def create_test_routing_logs():
    """创建测试路由日志数据"""
    
    await database.connect()
    
    try:
        print("=== 创建测试路由日志数据 ===\n")
        
        # 测试数据
        test_logs = [
            {
                "merchant_id": "merchant_high_risk_001",
                "agent_id": "agent_ee38f2b3645a2ec2",
                "order_id": f"test_order_{int(datetime.now().timestamp())}",
                "chosen_psp": "stripe",
                "conflict_detected": False,
                "resolution_method": "consensus",
                "decision_trace": json.dumps([
                    {"step": "initial_psps", "psps": ["stripe", "adyen", "paypal"]},
                    {"step": "merchant_rules_applied", "output_psps": ["stripe"]},
                    {"step": "agent_rules_applied", "output_psps": ["stripe"]}
                ]),
                "merchant_rules_applied": json.dumps({"excluded": ["paypal", "square"], "required": ["stripe"]}),
                "agent_rules_applied": json.dumps({"preferred": ["stripe", "adyen", "paypal"]}),
                "execution_time_ms": 15,
                "created_at": datetime.now() - timedelta(hours=2)
            },
            {
                "merchant_id": "merchant_cost_sensitive_002",
                "agent_id": "agent_ee38f2b3645a2ec2",
                "order_id": f"test_order_{int(datetime.now().timestamp()) + 1}",
                "chosen_psp": "stripe",
                "conflict_detected": True,
                "resolution_method": "merchant_priority",
                "decision_trace": json.dumps([
                    {"step": "initial_psps", "psps": ["stripe", "adyen", "paypal"]},
                    {"step": "merchant_rules_applied", "output_psps": ["stripe", "paypal"]},
                    {"action": "agent_excluded", "psp": "adyen", "reason": "conflict detected"}
                ]),
                "merchant_rules_applied": json.dumps({"excluded": ["adyen"], "preferred": ["paypal", "stripe"]}),
                "agent_rules_applied": json.dumps({"preferred": ["stripe", "adyen"], "weights": {"stripe": 1.0, "adyen": 0.85}}),
                "execution_time_ms": 23,
                "created_at": datetime.now() - timedelta(hours=1)
            },
            {
                "merchant_id": "merchant_high_risk_001",
                "agent_id": "agent_ee38f2b3645a2ec2",
                "order_id": f"test_order_{int(datetime.now().timestamp()) + 2}",
                "chosen_psp": "stripe",
                "conflict_detected": False,
                "resolution_method": "consensus",
                "decision_trace": json.dumps([
                    {"step": "initial_psps", "psps": ["stripe", "adyen"]},
                    {"step": "final_selection", "selected": "stripe"}
                ]),
                "merchant_rules_applied": json.dumps({"required": ["stripe"]}),
                "agent_rules_applied": json.dumps({"preferred": ["stripe"]}),
                "execution_time_ms": 12,
                "created_at": datetime.now() - timedelta(minutes=30)
            },
            {
                "merchant_id": "merchant_cost_sensitive_002",
                "agent_id": "agent_ee38f2b3645a2ec2",
                "order_id": f"test_order_{int(datetime.now().timestamp()) + 3}",
                "chosen_psp": "paypal",
                "conflict_detected": False,
                "resolution_method": "consensus",
                "decision_trace": json.dumps([
                    {"step": "initial_psps", "psps": ["stripe", "paypal"]},
                    {"step": "agent_preference", "selected": "paypal"}
                ]),
                "merchant_rules_applied": json.dumps({"preferred": ["paypal", "stripe"]}),
                "agent_rules_applied": json.dumps({"preferred": ["paypal", "stripe"]}),
                "execution_time_ms": 18,
                "created_at": datetime.now() - timedelta(minutes=10)
            }
        ]
        
        # 插入测试日志
        for i, log in enumerate(test_logs, 1):
            query = """
                INSERT INTO routing_logs (
                    merchant_id, agent_id, order_id, 
                    considered_psps, chosen_psp, decision_trace,
                    merchant_rules_applied, agent_rules_applied,
                    conflict_detected, resolution_method,
                    execution_time_ms, created_at
                ) VALUES (
                    :merchant_id, :agent_id, :order_id,
                    :considered_psps, :chosen_psp, :decision_trace,
                    :merchant_rules_applied, :agent_rules_applied,
                    :conflict_detected, :resolution_method,
                    :execution_time_ms, :created_at
                )
            """
            
            await database.execute(query, {
                **log,
                "considered_psps": json.dumps(["stripe", "adyen", "paypal"])
            })
            
            print(f"✅ 日志 {i}: {log['chosen_psp']} | 冲突: {log['conflict_detected']} | {log['resolution_method']}")
        
        print(f"\n成功创建 {len(test_logs)} 条测试路由日志！")
        
        # 验证数据
        count = await database.fetch_one("SELECT COUNT(*) as count FROM routing_logs")
        conflicts = await database.fetch_one("SELECT COUNT(*) as count FROM routing_logs WHERE conflict_detected = true")
        
        print(f"\n数据库统计:")
        print(f"  总路由记录: {count['count']}")
        print(f"  冲突记录: {conflicts['count']}")
        
        # PSP分布
        psp_dist = await database.fetch_all("""
            SELECT chosen_psp, COUNT(*) as count
            FROM routing_logs
            GROUP BY chosen_psp
            ORDER BY count DESC
        """)
        
        print(f"\nPSP 使用分布:")
        for row in psp_dist:
            print(f"  {row['chosen_psp']}: {row['count']} 次")
        
    except Exception as e:
        print(f"❌ 错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await database.disconnect()

if __name__ == "__main__":
    asyncio.run(create_test_routing_logs())

