"""
Auto KYB Validator - 快速自动审批逻辑
验证 Store URL 是否有效，以及与商家名称是否匹配
"""

import httpx
import re
from typing import Dict, Tuple
from urllib.parse import urlparse

async def validate_store_url(store_url: str) -> Tuple[bool, str]:
    """
    验证 Store URL 是否可访问
    
    Returns:
        (is_valid, message)
    """
    try:
        # 1. 基本格式验证
        parsed = urlparse(store_url)
        if not parsed.scheme in ['http', 'https']:
            return False, "URL must use http or https protocol"
        
        if not parsed.netloc:
            return False, "Invalid URL format"
        
        # 2. 检查是否是常见电商平台
        common_platforms = [
            'shopify.com',
            'myshopify.com',
            'wixsite.com',
            'wix.com',
            'bigcommerce.com',
            'squarespace.com',
            'webflow.io'
        ]
        
        is_known_platform = any(platform in parsed.netloc.lower() for platform in common_platforms)
        
        # 3. 尝试访问 URL（设置较短的超时）
        try:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
                response = await client.head(store_url)
                if response.status_code < 400:
                    return True, f"Store URL accessible (Status: {response.status_code})"
                else:
                    # 即使返回 4xx，如果是已知平台也接受
                    if is_known_platform:
                        return True, f"Known e-commerce platform detected: {parsed.netloc}"
                    return False, f"Store URL returned error: {response.status_code}"
        except httpx.TimeoutException:
            # 超时但如果是已知平台，仍然接受
            if is_known_platform:
                return True, f"Known e-commerce platform: {parsed.netloc} (timeout but accepted)"
            return False, "Store URL timeout - unable to verify"
        except httpx.RequestError as e:
            # 网络错误但如果是已知平台，仍然接受
            if is_known_platform:
                return True, f"Known e-commerce platform: {parsed.netloc} (network error but accepted)"
            return False, f"Unable to access store URL: {str(e)}"
            
    except Exception as e:
        return False, f"Invalid URL: {str(e)}"


def check_name_url_match(business_name: str, store_url: str) -> Tuple[bool, str, float]:
    """
    检查商家名称和 Store URL 是否匹配
    
    Returns:
        (is_match, message, confidence_score)
    """
    try:
        # 标准化商家名称：移除特殊字符，转小写
        normalized_name = re.sub(r'[^a-z0-9]', '', business_name.lower())
        
        # 从 URL 提取域名部分
        parsed = urlparse(store_url)
        domain = parsed.netloc.lower()
        
        # 移除常见前缀和后缀
        domain_clean = domain.replace('www.', '').replace('.com', '').replace('.net', '').replace('.store', '')
        domain_clean = re.sub(r'[^a-z0-9]', '', domain_clean)
        
        # 检查是否是平台店铺（shopify, wix 等）
        if 'shopify.com' in domain or 'myshopify' in domain:
            # 提取 shopify 店铺名称
            shop_name = domain.split('.')[0]
            shop_name_clean = re.sub(r'[^a-z0-9]', '', shop_name)
            
            # 计算相似度
            if normalized_name in shop_name_clean or shop_name_clean in normalized_name:
                return True, f"Shopify store name matches business name", 0.9
            else:
                # 计算 Levenshtein 距离的简化版本
                similarity = calculate_similarity(normalized_name, shop_name_clean)
                if similarity > 0.6:
                    return True, f"Shopify store name similar to business name (score: {similarity:.2f})", similarity
                else:
                    return False, f"Shopify store name doesn't match business name (score: {similarity:.2f})", similarity
        
        # 检查独立域名
        elif normalized_name in domain_clean or domain_clean in normalized_name:
            return True, f"Domain matches business name", 0.95
        else:
            similarity = calculate_similarity(normalized_name, domain_clean)
            if similarity > 0.5:
                return True, f"Domain similar to business name (score: {similarity:.2f})", similarity
            else:
                # 不强制要求匹配，但给出警告
                return True, f"⚠️ Domain and business name mismatch (score: {similarity:.2f}) - Manual review recommended", similarity
                
    except Exception as e:
        return True, f"Unable to verify match: {str(e)} - Accepted for manual review", 0.5


def calculate_similarity(str1: str, str2: str) -> float:
    """简单的字符串相似度计算（基于公共子串）"""
    if not str1 or not str2:
        return 0.0
    
    # 找最长公共子串
    max_len = 0
    for i in range(len(str1)):
        for j in range(len(str2)):
            k = 0
            while (i + k < len(str1) and j + k < len(str2) and 
                   str1[i + k] == str2[j + k]):
                k += 1
            max_len = max(max_len, k)
    
    # 相似度 = 公共子串长度 / 较短字符串长度
    shorter_len = min(len(str1), len(str2))
    return max_len / shorter_len if shorter_len > 0 else 0.0


async def auto_kyb_pre_approval(business_name: str, store_url: str, region: str) -> Dict:
    """
    自动 KYB 预审批
    
    Returns:
        {
            "approved": bool,
            "confidence_score": float,
            "validation_results": dict,
            "message": str,
            "requires_full_kyb": bool,  # 是否需要在7天内完成完整KYB
            "full_kyb_deadline": str  # ISO格式时间戳
        }
    """
    from datetime import datetime, timedelta
    
    results = {
        "approved": False,
        "confidence_score": 0.0,
        "validation_results": {},
        "message": "",
        "requires_full_kyb": True,
        "full_kyb_deadline": (datetime.now() + timedelta(days=7)).isoformat()
    }
    
    # 1. 验证 Store URL
    url_valid, url_message = await validate_store_url(store_url)
    results["validation_results"]["url_validation"] = {
        "valid": url_valid,
        "message": url_message
    }
    
    # 2. 验证名称匹配
    name_match, match_message, match_score = check_name_url_match(business_name, store_url)
    results["validation_results"]["name_match"] = {
        "match": name_match,
        "message": match_message,
        "score": match_score
    }
    
    # 3. 计算总体置信度
    url_score = 1.0 if url_valid else 0.0
    total_score = (url_score * 0.6 + match_score * 0.4)
    results["confidence_score"] = total_score
    
    # 4. 决定是否自动批准
    # 策略：URL 必须有效，名称匹配度 > 0.3 即可自动批准（宽松策略）
    if url_valid and match_score > 0.3:
        results["approved"] = True
        results["message"] = (
            f"✅ Auto-approved for quick start (confidence: {total_score:.0%})\n"
            f"Store URL verified: {url_message}\n"
            f"Name match: {match_message}\n"
            f"⚠️ Full KYB documentation required within 7 days"
        )
    else:
        results["approved"] = False
        results["message"] = (
            f"❌ Auto-approval failed (confidence: {total_score:.0%})\n"
            f"URL: {url_message}\n"
            f"Name: {match_message}\n"
            f"Manual admin review required"
        )
    
    return results

