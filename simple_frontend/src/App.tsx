import React from 'react';
import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import { AuthProvider, useAuth } from './contexts/AuthContext';
import { ProtectedRoute } from './components/ProtectedRoute';
import { Layout } from './components/Layout';
import { LoginPage } from './pages/LoginPage';
import { AdminDashboard } from './pages/AdminDashboard';
import { EmployeeDashboard } from './pages/EmployeeDashboard';
import { MerchantOnboardingDashboard } from './components/MerchantOnboardingDashboard';

const AppRoutes: React.FC = () => {
  const { user, loading } = useAuth();

  if (loading) {
    return (
      <div className="flex justify-center items-center min-h-screen">
        <div className="text-lg">Loading...</div>
      </div>
    );
  }

  if (!user) {
    return (
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    );
  }

  // Route based on user role
  const defaultRoute = user?.role && ['employee', 'admin', 'super_admin'].includes(user.role) 
    ? '/employee' 
    : '/admin';

  return (
    <Routes>
      <Route path="/employee" element={
        <ProtectedRoute requireAdmin={false}>
          <EmployeeDashboard />
        </ProtectedRoute>
      } />
      <Route path="/admin" element={
        <Layout>
          <ProtectedRoute requireAdmin={true}>
            <AdminDashboard />
          </ProtectedRoute>
        </Layout>
      } />
      <Route path="/admin/users" element={
        <Layout>
          <ProtectedRoute requireAdmin={true}>
            <AdminDashboard />
          </ProtectedRoute>
        </Layout>
      } />
      <Route path="/admin/psp" element={
        <Layout>
          <ProtectedRoute requireAdmin={true}>
            <AdminDashboard />
          </ProtectedRoute>
        </Layout>
      } />
      <Route path="/merchant/onboarding" element={<MerchantOnboardingDashboard />} />
      <Route path="/login" element={<Navigate to={defaultRoute} replace />} />
      <Route path="/" element={<Navigate to={defaultRoute} replace />} />
      <Route path="*" element={<Navigate to={defaultRoute} replace />} />
    </Routes>
  );
};

const App: React.FC = () => {
  return (
    <Router>
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </Router>
  );
};

export default App;




const App: React.FC = () => {
  return (
    <Router>
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </Router>
  );
};

export default App;


