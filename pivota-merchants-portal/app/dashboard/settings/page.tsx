'use client';

import { useState, useEffect } from 'react';
import { User, Bell, Shield, Save, Loader2 } from 'lucide-react';
import { apiClient } from '@/lib/api-client';

export default function SettingsPage() {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  
  const [profile, setProfile] = useState({
    business_name: '',
    contact_email: '',
    contact_phone: '',
    website: '',
    address: '',
  });
  
  const [notifications, setNotifications] = useState({
    email_orders: true,
    email_payments: true,
    email_inventory: false,
    email_weekly: false,
  });

  useEffect(() => {
    loadSettings();
  }, []);

  const loadSettings = async () => {
    try {
      setLoading(true);
      const data = await apiClient.getProfile();
      if (data) {
        setProfile({
          business_name: data.business_name || '',
          contact_email: data.contact_email || '',
          contact_phone: data.contact_phone || '',
          website: data.website || '',
          address: data.address || '',
        });
      }
    } catch (error) {
      console.error('Failed to load settings:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const result = await apiClient.updateProfile(profile);
      alert(result.message || '✅ Settings saved successfully!');
    } catch (error: any) {
      alert('❌ Failed to save: ' + (error.response?.data?.detail || error.message));
    } finally {
      setSaving(false);
    }
  };

  const handleChangePassword = () => {
    const oldPassword = prompt('Enter your current password:');
    if (!oldPassword) {
      // User cancelled or entered empty string
      return;
    }
    
    const newPassword = prompt('Enter your new password:');
    if (!newPassword) {
      // User cancelled or entered empty string
      return;
    }
    
    const confirmPassword = prompt('Confirm your new password:');
    if (!confirmPassword) {
      // User cancelled or entered empty string
      return;
    }
    
    if (newPassword !== confirmPassword) {
      alert('❌ Passwords do not match');
      return;
    }
    
    if (newPassword.length < 8) {
      alert('❌ Password must be at least 8 characters long');
      return;
    }
    
    handleChangePasswordAsync(oldPassword, newPassword);
  };

  const handleChangePasswordAsync = async (oldPassword: string, newPassword: string) => {
    try {
      const result = await apiClient.changePassword({ old_password: oldPassword, new_password: newPassword });
      alert(result.message || '✅ Password changed successfully!');
    } catch (error: any) {
      alert('❌ Failed to change password: ' + (error.response?.data?.detail || error.message));
    }
  };

  const handleEnable2FA = async () => {
    try {
      const result = await apiClient.enable2FA();
      if (result.data?.qr_code_url) {
        alert('✅ 2FA enabled! Please scan the QR code with your authenticator app.\n\nQR Code: ' + result.data.qr_code_url);
      } else {
        alert(result.message || '✅ 2FA enabled successfully!');
      }
    } catch (error: any) {
      alert('❌ Failed to enable 2FA: ' + (error.response?.data?.detail || error.message));
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6 max-w-4xl">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Settings</h1>
        <p className="text-gray-600">Manage your store settings and preferences</p>
      </div>

      {/* Business Profile */}
      <div className="bg-white rounded-lg shadow">
        <div className="px-6 py-4 border-b flex items-center">
          <User className="w-5 h-5 text-gray-400 mr-3" />
          <h2 className="text-lg font-semibold">Business Profile</h2>
        </div>
        <div className="p-6 space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-2">
                Business Name
              </label>
              <input
                type="text"
                value={profile.business_name}
                onChange={(e) => setProfile({ ...profile, business_name: e.target.value })}
                className="w-full px-3 py-2 border rounded-lg"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-2">
                Contact Email
              </label>
              <input
                type="email"
                value={profile.contact_email}
                onChange={(e) => setProfile({ ...profile, contact_email: e.target.value })}
                className="w-full px-3 py-2 border rounded-lg"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-2">
                Phone Number
              </label>
              <input
                type="tel"
                value={profile.contact_phone}
                onChange={(e) => setProfile({ ...profile, contact_phone: e.target.value })}
                className="w-full px-3 py-2 border rounded-lg"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-2">
                Website
              </label>
              <input
                type="url"
                value={profile.website}
                onChange={(e) => setProfile({ ...profile, website: e.target.value })}
                className="w-full px-3 py-2 border rounded-lg"
                placeholder="https://example.com"
              />
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Business Address
            </label>
            <textarea
              value={profile.address}
              onChange={(e) => setProfile({ ...profile, address: e.target.value })}
              className="w-full px-3 py-2 border rounded-lg"
              rows={3}
            />
          </div>
        </div>
      </div>

      {/* Notifications */}
      <div className="bg-white rounded-lg shadow">
        <div className="px-6 py-4 border-b flex items-center">
          <Bell className="w-5 h-5 text-gray-400 mr-3" />
          <h2 className="text-lg font-semibold">Email Notifications</h2>
        </div>
        <div className="p-6 space-y-4">
          <label className="flex items-center justify-between">
            <div>
              <div className="font-medium">New Orders</div>
              <div className="text-sm text-gray-600">Get notified when you receive new orders</div>
            </div>
            <input
              type="checkbox"
              checked={notifications.email_orders}
              onChange={(e) => setNotifications({ ...notifications, email_orders: e.target.checked })}
              className="rounded"
            />
          </label>
          <label className="flex items-center justify-between">
            <div>
              <div className="font-medium">Payment Updates</div>
              <div className="text-sm text-gray-600">Notifications about payment status changes</div>
            </div>
            <input
              type="checkbox"
              checked={notifications.email_payments}
              onChange={(e) => setNotifications({ ...notifications, email_payments: e.target.checked })}
              className="rounded"
            />
          </label>
          <label className="flex items-center justify-between">
            <div>
              <div className="font-medium">Low Inventory</div>
              <div className="text-sm text-gray-600">Alert when products are running low</div>
            </div>
            <input
              type="checkbox"
              checked={notifications.email_inventory}
              onChange={(e) => setNotifications({ ...notifications, email_inventory: e.target.checked })}
              className="rounded"
            />
          </label>
          <label className="flex items-center justify-between">
            <div>
              <div className="font-medium">Weekly Reports</div>
              <div className="text-sm text-gray-600">Receive weekly performance summaries</div>
            </div>
            <input
              type="checkbox"
              checked={notifications.email_weekly}
              onChange={(e) => setNotifications({ ...notifications, email_weekly: e.target.checked })}
              className="rounded"
            />
          </label>
        </div>
      </div>

      {/* Security */}
      <div className="bg-white rounded-lg shadow">
        <div className="px-6 py-4 border-b flex items-center">
          <Shield className="w-5 h-5 text-gray-400 mr-3" />
          <h2 className="text-lg font-semibold">Security</h2>
        </div>
        <div className="p-6 space-y-4">
          <div className="flex items-center justify-between p-4 bg-gray-50 rounded-lg">
            <div>
              <div className="font-medium">Two-Factor Authentication</div>
              <div className="text-sm text-gray-600">Add an extra layer of security</div>
            </div>
            <button 
              onClick={handleEnable2FA}
              className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
            >
              Enable
            </button>
          </div>
          <div className="flex items-center justify-between p-4 bg-gray-50 rounded-lg">
            <div>
              <div className="font-medium">Change Password</div>
              <div className="text-sm text-gray-600">Update your account password</div>
            </div>
            <button 
              onClick={handleChangePassword}
              className="px-4 py-2 border rounded-lg hover:bg-gray-100"
            >
              Change
            </button>
          </div>
        </div>
      </div>

      {/* Save Button */}
      <div className="flex justify-end">
        <button
          onClick={handleSave}
          disabled={saving}
          className="flex items-center space-x-2 px-6 py-3 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50"
        >
          {saving ? (
            <>
              <Loader2 className="w-5 h-5 animate-spin" />
              <span>Saving...</span>
            </>
          ) : (
            <>
              <Save className="w-5 h-5" />
              <span>Save Changes</span>
            </>
          )}
        </button>
      </div>
    </div>
  );
}

