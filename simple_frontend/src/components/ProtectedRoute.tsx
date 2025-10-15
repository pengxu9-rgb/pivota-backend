import React, { ReactNode } from 'react';
import { useAuth } from '../contexts/AuthContext';

interface ProtectedRouteProps {
  children: ReactNode;
  requireAdmin?: boolean;
}

export const ProtectedRoute: React.FC<ProtectedRouteProps> = ({ 
  children, 
  requireAdmin = false 
}) => {
  const { user, loading, isAdmin } = useAuth();

  // Debug logging
  console.log('🔒 ProtectedRoute check:', {
    user,
    loading,
    isAdmin,
    requireAdmin,
    userRole: user?.role
  });

  if (loading) {
    return (
      <div className="flex justify-center items-center min-h-screen">
        <div className="text-lg">Loading...</div>
      </div>
    );
  }

  if (!user) {
    console.log('❌ No user, redirecting to login');
    window.location.href = '/login';
    return null;
  }

  if (requireAdmin && !isAdmin) {
    console.log('❌ Access denied - requireAdmin:', requireAdmin, 'isAdmin:', isAdmin, 'user.role:', user.role);
    return (
      <div className="flex justify-center items-center min-h-screen">
        <div className="text-center">
          <h1 className="text-2xl font-bold text-red-600 mb-4">Access Denied</h1>
          <p className="text-gray-600">You need admin privileges to access this page.</p>
          <p className="text-sm text-gray-500 mt-4">Your role: {user.role}</p>
          <p className="text-sm text-gray-500">Required: admin</p>
        </div>
      </div>
    );
  }

  console.log('✅ Access granted');
  return <>{children}</>;
};


