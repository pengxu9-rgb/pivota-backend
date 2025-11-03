import React, { ReactNode } from 'react';
import { useAuth } from '../contexts/AuthContext';
import { LogOut, User, Settings, BarChart3 } from 'lucide-react';

interface LayoutProps {
  children: ReactNode;
}

export const Layout: React.FC<LayoutProps> = ({ children }) => {
  const { user, signout } = useAuth();

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white shadow-sm border-b">
        <div className="container mx-auto px-4">
          <div className="flex justify-between items-center h-16">
            <div className="flex items-center gap-4">
              <h1 className="text-xl font-bold text-blue-600">Pivota Admin</h1>
              <nav className="hidden md:flex gap-6">
                <a href="/admin" className="text-gray-600 hover:text-blue-600 flex items-center gap-2">
                  <BarChart3 size={16} />
                  Dashboard
                </a>
                <a href="/admin/users" className="text-gray-600 hover:text-blue-600 flex items-center gap-2">
                  <User size={16} />
                  Users
                </a>
                <a href="/admin/psp" className="text-gray-600 hover:text-blue-600 flex items-center gap-2">
                  <Settings size={16} />
                  PSP
                </a>
              </nav>
            </div>
            
            <div className="flex items-center gap-4">
              <div className="text-sm text-gray-600">
                <div className="font-medium">{user?.email}</div>
                <div className="text-xs capitalize">{user?.role}</div>
              </div>
              <button
                onClick={signout}
                className="btn btn-secondary btn-sm flex items-center gap-2"
              >
                <LogOut size={16} />
                Sign Out
              </button>
            </div>
          </div>
        </div>
      </header>

      {/* Main Content */}
      <main className="container mx-auto px-4 py-6">
        {children}
      </main>
    </div>
  );
};


