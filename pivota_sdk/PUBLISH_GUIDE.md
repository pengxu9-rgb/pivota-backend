# SDK Publishing Guide

## Python SDK Publishing

### Prerequisites
```bash
pip install build twine
```

### Build the package
```bash
cd pivota_sdk/python
python -m build
```

### Test with TestPyPI (recommended first)
```bash
# Upload to TestPyPI
python -m twine upload --repository testpypi dist/*

# Test installation
pip install --index-url https://test.pypi.org/simple/ pivota-agent
```

### Publish to PyPI
```bash
python -m twine upload dist/*
```

### Credentials Needed
- PyPI account: https://pypi.org/account/register/
- Create API token: https://pypi.org/manage/account/token/
- Use `__token__` as username and token as password

---

## TypeScript SDK Publishing

### Prerequisites
```bash
npm install -g npm
```

### Build the package
```bash
cd pivota_sdk/typescript
npm run build
```

### Login to npm
```bash
npm login
```

### Publish
```bash
# Test publish (dry-run)
npm publish --dry-run

# Actual publish
npm publish --access public
```

### Credentials Needed
- npm account: https://www.npmjs.com/signup
- Login with `npm login`

---

## MCP Server Publishing

### Prerequisites
```bash
cd pivota_sdk/mcp-server
npm install
```

### Build and publish
```bash
npm run build
npm publish --access public
```

---

## Post-Publishing Checklist

- [ ] Test Python SDK: `pip install pivota-agent`
- [ ] Test TypeScript SDK: `npm install pivota-agent`
- [ ] Test MCP Server: `npx pivota-mcp-server`
- [ ] Update Integration page with published package names
- [ ] Update documentation links
- [ ] Create GitHub releases
- [ ] Update version numbers for future releases


