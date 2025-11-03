-- Agent Portal 用户创建 SQL 脚本
-- 在 Railway PostgreSQL Query 控制台中执行

-- 1. 检查现有的 agent 记录
SELECT 
  agent_id, 
  name, 
  email, 
  status,
  api_key 
FROM agents 
WHERE agent_id = 'agent_ee38f2b3645a2ec2';

-- 2. 检查是否已有对应的 users 记录
SELECT 
  id, 
  email, 
  role, 
  active 
FROM users 
WHERE email = 'asdf@asdf.com' OR email = 'agent@pivota.com';

-- 3. 为现有的 agent (agent_ee38f2b3645a2ec2) 创建登录账户
-- 使用 agent 表中的 email: asdf@asdf.com
-- Password: Agent123456

INSERT INTO users (email, password_hash, full_name, role, active, created_at)
VALUES (
  'asdf@asdf.com',
  -- Password: Agent123456 (bcrypt hashed)
  '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5ViT0VrKZOBBu',
  'asdf',
  'agent',
  true,
  NOW()
)
ON CONFLICT (email) DO UPDATE SET
  role = 'agent',
  password_hash = '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5ViT0VrKZOBBu',
  active = true
RETURNING id, email, role;

-- 4. 验证创建成功
SELECT 
  u.id as user_id,
  u.email,
  u.role,
  u.active,
  a.agent_id,
  a.name as agent_name,
  a.status as agent_status
FROM users u
INNER JOIN agents a ON u.email = a.email
WHERE u.email = 'asdf@asdf.com';

-- 期望结果:
-- user_id | email           | role  | active | agent_id                   | agent_name | agent_status
-- --------|-----------------|-------|--------|----------------------------|------------|-------------
-- xxx     | asdf@asdf.com   | agent | true   | agent_ee38f2b3645a2ec2     | asdf       | active

-- 完成！现在可以使用以下凭据登录 Agent Portal:
-- Email: asdf@asdf.com
-- Password: Agent123456

