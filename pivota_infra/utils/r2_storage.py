"""
Cloudflare R2 Storage Service
S3-compatible object storage for KYC documents
"""

import boto3
import os
import uuid
from botocore.config import Config
from typing import Optional, Tuple
import logging

logger = logging.getLogger(__name__)

# R2 配置（从环境变量读取）
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME", "pivota-kyc-documents")

# R2 endpoint format: https://<ACCOUNT_ID>.r2.cloudflarestorage.com
R2_ENDPOINT = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else None

# 初始化 S3 客户端（兼容 R2）
s3_client = None

def init_r2_client():
    """初始化 R2 客户端"""
    global s3_client
    
    if not all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY]):
        logger.warning("⚠️ R2 credentials not configured. File storage will be disabled.")
        return None
    
    try:
        s3_client = boto3.client(
            's3',
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            config=Config(signature_version='s3v4'),
            region_name='auto'  # R2 uses 'auto' for region
        )
        logger.info(f"✅ R2 client initialized for bucket: {R2_BUCKET_NAME}")
        return s3_client
    except Exception as e:
        logger.error(f"❌ Failed to initialize R2 client: {e}")
        return None


async def upload_file_to_r2(
    file_content: bytes,
    filename: str,
    merchant_id: str,
    content_type: str = "application/octet-stream"
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    上传文件到 R2
    
    Args:
        file_content: 文件内容（字节）
        filename: 原始文件名
        merchant_id: 商户 ID
        content_type: MIME 类型
    
    Returns:
        (success, file_key, error_message)
        file_key 格式: kyc/{merchant_id}/{uuid}_{filename}
    """
    global s3_client
    
    if s3_client is None:
        s3_client = init_r2_client()
    
    if s3_client is None:
        return False, None, "R2 storage not configured"
    
    try:
        # 生成唯一文件键
        file_uuid = uuid.uuid4().hex[:8]
        file_key = f"kyc/{merchant_id}/{file_uuid}_{filename}"
        
        # 上传到 R2
        s3_client.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=file_key,
            Body=file_content,
            ContentType=content_type,
            Metadata={
                'merchant_id': merchant_id,
                'original_filename': filename
            }
        )
        
        logger.info(f"✅ File uploaded to R2: {file_key}")
        return True, file_key, None
        
    except Exception as e:
        logger.error(f"❌ Failed to upload file to R2: {e}")
        return False, None, str(e)


async def get_file_from_r2(file_key: str) -> Tuple[bool, Optional[bytes], Optional[str], Optional[str]]:
    """
    从 R2 下载文件
    
    Args:
        file_key: R2 中的文件键
    
    Returns:
        (success, file_content, content_type, error_message)
    """
    global s3_client
    
    if s3_client is None:
        s3_client = init_r2_client()
    
    if s3_client is None:
        return False, None, None, "R2 storage not configured"
    
    try:
        response = s3_client.get_object(Bucket=R2_BUCKET_NAME, Key=file_key)
        file_content = response['Body'].read()
        content_type = response.get('ContentType', 'application/octet-stream')
        
        logger.info(f"✅ File downloaded from R2: {file_key}")
        return True, file_content, content_type, None
        
    except s3_client.exceptions.NoSuchKey:
        logger.warning(f"⚠️ File not found in R2: {file_key}")
        return False, None, None, "File not found"
    except Exception as e:
        logger.error(f"❌ Failed to download file from R2: {e}")
        return False, None, None, str(e)


async def get_presigned_url(file_key: str, expiration: int = 3600) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    生成预签名 URL（用于临时访问）
    
    Args:
        file_key: R2 中的文件键
        expiration: URL 有效期（秒），默认 1 小时
    
    Returns:
        (success, presigned_url, error_message)
    """
    global s3_client
    
    if s3_client is None:
        s3_client = init_r2_client()
    
    if s3_client is None:
        return False, None, "R2 storage not configured"
    
    try:
        url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': R2_BUCKET_NAME, 'Key': file_key},
            ExpiresIn=expiration
        )
        
        logger.info(f"✅ Presigned URL generated for: {file_key}")
        return True, url, None
        
    except Exception as e:
        logger.error(f"❌ Failed to generate presigned URL: {e}")
        return False, None, str(e)


async def delete_file_from_r2(file_key: str) -> Tuple[bool, Optional[str]]:
    """
    从 R2 删除文件
    
    Args:
        file_key: R2 中的文件键
    
    Returns:
        (success, error_message)
    """
    global s3_client
    
    if s3_client is None:
        s3_client = init_r2_client()
    
    if s3_client is None:
        return False, "R2 storage not configured"
    
    try:
        s3_client.delete_object(Bucket=R2_BUCKET_NAME, Key=file_key)
        logger.info(f"✅ File deleted from R2: {file_key}")
        return True, None
        
    except Exception as e:
        logger.error(f"❌ Failed to delete file from R2: {e}")
        return False, str(e)


# 初始化客户端（应用启动时调用）
def startup():
    """应用启动时初始化 R2 客户端"""
    init_r2_client()

