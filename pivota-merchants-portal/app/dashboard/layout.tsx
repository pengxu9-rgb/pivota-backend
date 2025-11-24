"use client";

import { useState, useEffect } from "react";
import { useRouter, usePathname } from "next/navigation";
import Link from "next/link";
import {
  Store,
  LayoutDashboard,
  ShoppingBag,
  Package,
  Link as LinkIcon,
  Settings,
  LogOut,
  Menu,
  X,
  BarChart3,
  Zap,
  DollarSign,
} from "lucide-react";
import { apiClient } from "@/lib/api-client";
import { FEATURE_FLAGS } from "@/lib/config";

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const [user, setUser] = useState<any>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Check authentication
    const token = localStorage.getItem("merchant_token");
    const userData = localStorage.getItem("merchant_user");

    if (!token) {
      router.push("/login");
      return;
    }

    if (userData) {
      setUser(JSON.parse(userData));
    }

    setLoading(false);
  }, [router]);

  const handleLogout = () => {
    apiClient.logout();
  };

  const navigation = [
    { name: "Dashboard", href: "/dashboard", icon: LayoutDashboard },
    { name: "Orders", href: "/dashboard/orders", icon: ShoppingBag },
    { name: "Products", href: "/dashboard/products", icon: Package },
    { name: "Commission", href: "/dashboard/commission", icon: DollarSign },
    { name: "Integrations", href: "/dashboard/integrations", icon: LinkIcon },
    { name: "Analytics", href: "/dashboard/analytics", icon: BarChart3 },
    { name: "MCP", href: "/dashboard/mcp", icon: Zap },
    { name: "Payouts", href: "/dashboard/payouts", icon: DollarSign },
    { name: "Settings", href: "/dashboard/settings", icon: Settings },
    // Platform Merchant Onboarding v2 (feature-flagged entry)
    ...(FEATURE_FLAGS.PLATFORM_ONBOARDING_V2
      ? [{ name: "Platform Onboarding", href: "/dashboard/platform-onboarding", icon: Zap }]
      : []),
    // Platform Orders (feature-flagged entry)
    ...(FEATURE_FLAGS.PLATFORM_ORDERS_V1
      ? [{ name: "Platform Orders", href: "/dashboard/platform-orders", icon: ShoppingBag }]
      : []),
  ];

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600"></div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Mobile sidebar backdrop */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 bg-black bg-opacity-50 z-40 lg:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* Sidebar */}
      <div
        className={`fixed inset-y-0 left-0 z-50 w-64 bg-white shadow-lg transform transition-transform lg:translate-x-0 flex flex-col h-full ${
          sidebarOpen ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="flex-shrink-0">
          <div className="flex items-center justify-between h-14 px-4">
            <div className="flex items-center space-x-2">
              <Store className="w-6 h-6 text-blue-600" />
              <span className="text-lg font-bold text-gray-900">Pivota</span>
            </div>
            <button
              onClick={() => setSidebarOpen(false)}
              className="lg:hidden text-gray-500 hover:text-gray-700"
            >
              <X className="w-5 h-5" />
            </button>
          </div>
          {user && (
            <div className="px-4 py-3 bg-blue-50 border-y border-blue-100">
              <p className="text-sm font-semibold text-gray-900">
                {user.business_name || "Merchant"}
              </p>
              <div className="mt-1">
                <p className="text-xs text-gray-600 mb-0.5">Merchant ID:</p>
                <p className="text-xs text-gray-800 font-mono break-all select-all cursor-text bg-white/50 px-2 py-1 rounded">
                  {user.merchant_id || "N/A"}
                </p>
              </div>
            </div>
          )}
        </div>

        <nav className="flex-1 px-3 py-3 space-y-0.5 overflow-y-auto">
          {navigation.map((item) => {
            // Fix: Only highlight exact match or child pages (not sibling pages)
            const isActive =
              item.href === "/dashboard"
                ? pathname === "/dashboard"
                : pathname.startsWith(item.href);
            return (
              <Link
                key={item.name}
                href={item.href}
                className={`flex items-center space-x-2 px-3 py-1.5 rounded-md transition-colors text-sm ${
                  isActive
                    ? "bg-blue-50 text-blue-600"
                    : "text-gray-700 hover:bg-gray-100"
                }`}
              >
                <item.icon className="w-4 h-4 flex-shrink-0" />
                <span className="font-medium truncate">{item.name}</span>
              </Link>
            );
          })}
        </nav>

        <div className="flex-shrink-0 p-3 border-t">
          <button
            onClick={handleLogout}
            className="flex items-center space-x-2 w-full px-3 py-1.5 text-red-600 hover:bg-red-50 rounded-md transition-colors text-sm"
          >
            <LogOut className="w-4 h-4" />
            <span className="font-medium">Logout</span>
          </button>
        </div>
      </div>

      {/* Main content */}
      <div className="lg:pl-64">
        {/* Mobile menu button */}
        <div className="lg:hidden bg-white border-b">
          <button
            onClick={() => setSidebarOpen(true)}
            className="p-4 text-gray-500 hover:text-gray-700"
          >
            <Menu className="w-6 h-6" />
          </button>
        </div>

        {/* Page content */}
        <main className="p-6">{children}</main>
      </div>
    </div>
  );
}
