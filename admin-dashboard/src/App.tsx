import { useState, useEffect } from 'react';
import { merchantApi, Merchant } from './lib/api';
import StatsCards from './components/StatsCards';
import MerchantTable from './components/MerchantTable';
import TokenInput from './components/TokenInput';
import { Briefcase } from 'lucide-react';

function App() {
  const [merchants, setMerchants] = useState<Merchant[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchMerchants = async () => {
    try {
      setLoading(true);
      setError(null);
      const data = await merchantApi.getAll();
      setMerchants(data);
    } catch (err: any) {
      console.error('Failed to fetch merchants:', err);
      setError(err.response?.data?.detail || 'Failed to load merchants. Please check your JWT token.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchMerchants();
  }, []);

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100">
      {/* Header */}
      <header className="bg-white border-b border-slate-200 shadow-sm">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between h-16">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 bg-primary rounded-lg flex items-center justify-center">
                <Briefcase className="w-6 h-6 text-white" />
              </div>
              <div>
                <h1 className="text-xl font-bold text-slate-900">Pivota Admin</h1>
                <p className="text-sm text-slate-500">Merchant Management</p>
              </div>
            </div>
            <TokenInput onTokenSet={fetchMerchants} />
          </div>
        </div>
      </header>

      {/* Main Content */}
      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {error && (
          <div className="mb-6 p-4 bg-red-50 border border-red-200 rounded-lg">
            <p className="text-sm text-red-800">{error}</p>
            {error.includes('JWT') && (
              <p className="text-xs text-red-600 mt-1">
                💡 Paste your admin JWT token in the input field above
              </p>
            )}
          </div>
        )}

        <StatsCards merchants={merchants} loading={loading} />
        
        <div className="mt-8">
          <MerchantTable 
            merchants={merchants} 
            loading={loading} 
            onRefresh={fetchMerchants}
          />
        </div>
      </main>

      {/* Footer */}
      <footer className="mt-12 py-6 text-center text-sm text-slate-500">
        <p>Pivota Infrastructure Dashboard v1.0 • Connected to Railway</p>
      </footer>
    </div>
  );
}

export default App;


