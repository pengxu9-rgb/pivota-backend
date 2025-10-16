import { useState, useEffect } from 'react';
import { Key } from 'lucide-react';

interface TokenInputProps {
  onTokenSet: () => void;
}

export default function TokenInput({ onTokenSet }: TokenInputProps) {
  const [token, setToken] = useState('');
  const [showInput, setShowInput] = useState(false);

  useEffect(() => {
    const saved = localStorage.getItem('pivota_admin_jwt');
    if (saved) {
      setToken(saved);
    } else {
      setShowInput(true);
    }
  }, []);

  const handleSave = () => {
    localStorage.setItem('pivota_admin_jwt', token);
    setShowInput(false);
    onTokenSet();
  };

  const handleClear = () => {
    localStorage.removeItem('pivota_admin_jwt');
    setToken('');
    setShowInput(true);
  };

  if (!showInput && token) {
    return (
      <div className="flex items-center gap-2">
        <div className="flex items-center gap-2 px-3 py-1.5 bg-green-50 border border-green-200 rounded-md">
          <Key className="w-4 h-4 text-green-600" />
          <span className="text-xs font-medium text-green-700">Token Set</span>
        </div>
        <button
          onClick={() => setShowInput(true)}
          className="text-xs text-slate-600 hover:text-slate-900"
        >
          Change
        </button>
        <button
          onClick={handleClear}
          className="text-xs text-red-600 hover:text-red-900"
        >
          Clear
        </button>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-2">
      <input
        type="password"
        value={token}
        onChange={(e) => setToken(e.target.value)}
        placeholder="Paste Admin JWT Token..."
        className="w-80 px-3 py-1.5 text-sm border border-slate-300 rounded-md focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent"
      />
      <button
        onClick={handleSave}
        disabled={!token}
        className="px-4 py-1.5 text-sm font-medium text-white bg-primary rounded-md hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed"
      >
        Save
      </button>
    </div>
  );
}

