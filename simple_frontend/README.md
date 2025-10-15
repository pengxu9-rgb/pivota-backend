# Pivota Simple Frontend

A clean, modern React frontend for the Pivota payment infrastructure admin dashboard.

## Features

- ✅ **Authentication**: JWT-based auth with your existing FastAPI backend
- ✅ **Admin Dashboard**: Overview, PSP management, routing rules, merchants
- ✅ **PSP Management**: Test connections, toggle PSPs, view performance
- ✅ **User Management**: Approve users, change roles
- ✅ **Analytics**: Real-time metrics and performance data
- ✅ **System Logs**: View system activity and events
- ✅ **Responsive Design**: Works on desktop and mobile

## Quick Start

1. **Install dependencies:**
   ```bash
   cd simple_frontend
   npm install
   ```

2. **Start development server:**
   ```bash
   npm run dev
   ```

3. **Open in browser:**
   ```
   http://localhost:3000
   ```

## Backend Connection

The frontend connects to your existing FastAPI backend at:
```
https://pivota-dashboard.onrender.com
```

## Authentication

- Uses JWT tokens from your existing `/auth/signin` endpoint
- Stores tokens in localStorage
- Automatic token refresh and error handling
- Admin-only routes protection

## Available Routes

- `/login` - Sign in/up page
- `/admin` - Main admin dashboard
- `/admin/users` - User management
- `/admin/psp` - PSP management

## Key Components

- **AdminDashboard**: Main dashboard with tabs for different features
- **AuthContext**: Handles authentication state
- **ProtectedRoute**: Route protection for admin users
- **Layout**: Common layout with navigation

## API Integration

All API calls go through the `adminApi` service which:
- Automatically adds JWT tokens to requests
- Handles 401 errors by redirecting to login
- Provides typed interfaces for all endpoints

## Development

- Built with React 18 + TypeScript
- Uses Vite for fast development
- Tailwind CSS for styling
- Lucide React for icons

## Production Build

```bash
npm run build
```

The built files will be in the `dist/` directory, ready for deployment.

## Next Steps

Once this simple frontend is working perfectly with your backend, we can:

1. ✅ **Test all features** - PSP testing, user management, analytics
2. ✅ **Deploy to production** - Simple deployment to any hosting service
3. ✅ **Rebuild Lovable frontend** - Use this as a reference for the proper Lovable implementation

This approach keeps all your valuable backend infrastructure while providing a clean, working frontend immediately!


