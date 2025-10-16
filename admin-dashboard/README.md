# 🎨 Pivota Admin Dashboard

Modern, beautiful React dashboard for Pivota merchant management.

## ✨ Features

- 📊 Real-time merchant stats
- 🔍 Advanced filtering and search
- 🎯 Auto-approval indicators
- 📄 KYB document management
- ⚡ Fast and responsive UI
- 🎨 Beautiful Tailwind CSS design

## 🚀 Quick Start

### 1. Install Dependencies

```bash
cd admin-dashboard
npm install
```

### 2. Start Development Server

```bash
npm run dev
```

The dashboard will open at `http://localhost:5173`

### 3. Get Admin JWT Token

1. Go to https://web-production-fedb.up.railway.app/docs
2. Login as admin (if you have Supabase auth set up)
3. Copy the JWT token
4. Paste it in the dashboard's token input field (top right)

Or for testing, you can use the API directly to generate a token.

## 📦 Build for Production

```bash
npm run build
```

The built files will be in the `dist/` directory.

## 🚢 Deploy

### Option 1: Vercel (Recommended)

1. Push to GitHub
2. Go to https://vercel.com
3. Import your repository
4. Vercel will auto-detect Vite and deploy!

### Option 2: Netlify

1. Run `npm run build`
2. Drag the `dist/` folder to https://app.netlify.com/drop

### Option 3: Railway

```bash
# Add to your existing Railway project
railway link
railway up
```

## 🔧 Configuration

The API base URL is hardcoded in `src/lib/api.ts`:

```typescript
const API_BASE_URL = 'https://web-production-fedb.up.railway.app';
```

Change this if your backend URL is different.

## 📱 Tech Stack

- **React 18** - UI framework
- **TypeScript** - Type safety
- **Vite** - Build tool
- **Tailwind CSS** - Styling
- **Axios** - API client
- **Lucide React** - Icons
- **date-fns** - Date formatting

## 🎯 Usage

### 1. Set Admin Token

Paste your JWT token in the input field at the top of the dashboard.

### 2. View Merchants

- See all merchants in a beautiful table
- Filter by status (All, Approved, Pending, Rejected)
- Search by name, email, or ID

### 3. Manage Merchants

- **View Details**: See full merchant information
- **Review KYB**: Approve or reject merchants
- **Upload Docs**: Add KYB documents
- **Delete**: Soft delete merchants

## 🔐 Authentication

The dashboard uses JWT tokens stored in `localStorage`. The token is automatically added to all API requests.

## 📝 Notes

- Token is stored in localStorage as `pivota_admin_jwt`
- All API calls require authentication
- Auto-approval merchants are highlighted
- Confidence scores shown as percentages

## 🎨 Customization

### Colors

Edit `tailwind.config.js` to customize the color scheme.

### API Endpoints

All API logic is in `src/lib/api.ts` - modify as needed.

## 🐛 Troubleshooting

### "Not authenticated" errors

Make sure you've pasted a valid JWT token in the dashboard.

### CORS errors

Check that your backend allows requests from your frontend domain.

### Build errors

Run `npm install` to ensure all dependencies are installed.

## 📞 Support

For issues, check:
1. Browser console for errors
2. Network tab for failed requests
3. Backend logs on Railway

---

Built with ❤️ for Pivota

