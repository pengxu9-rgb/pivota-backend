'use client';

import { useState } from 'react';
import { 
  ArrowRight, 
  Settings as SettingsIcon,
  Globe,
  TrendingUp,
  Save,
} from 'lucide-react';

interface PSP {
  id: string;
  name: string;
  is_active: boolean;
}

interface Props {
  connectedPSPs: PSP[];
}

export default function PSPRoutingConfig({ connectedPSPs }: Props) {
  const [strategy, setStrategy] = useState('smart');
  const [primaryPSP, setPrimaryPSP] = useState('');
  const [fallbackChain, setFallbackChain] = useState<string[]>([]);
  const [geoRules, setGeoRules] = useState([
    { region: 'EU', psp: 'mollie' },
    { region: 'US', psp: 'stripe' },
  ]);

  const activePSPs = connectedPSPs.filter(p => p.is_active);

  const handleSaveRouting = () => {
    alert('✅ Routing configuration saved!\n\n' +
          `Strategy: ${strategy}\n` +
          `Primary PSP: ${primaryPSP}\n` +
          `Fallback: ${fallbackChain.join(' → ')}`);
  };

  return (
    <div className="space-y-6">
      {/* Routing Strategy */}
      <div className="bg-white rounded-lg shadow p-6">
        <div className="flex items-center space-x-3 mb-4">
          <SettingsIcon className="w-5 h-5 text-purple-600" />
          <h2 className="text-lg font-semibold text-gray-900">Routing Strategy</h2>
        </div>
        <div className="space-y-3">
          <label className="flex items-center justify-between p-3 border rounded-lg cursor-pointer hover:bg-gray-50">
            <div>
              <p className="font-medium text-gray-900">Smart AI Routing</p>
              <p className="text-sm text-gray-600">AI automatically routes to best PSP based on card type, location, amount</p>
            </div>
            <input
              type="radio"
              name="strategy"
              value="smart"
              checked={strategy === 'smart'}
              onChange={(e) => setStrategy(e.target.value)}
              className="w-4 h-4"
            />
          </label>
          <label className="flex items-center justify-between p-3 border rounded-lg cursor-pointer hover:bg-gray-50">
            <div>
              <p className="font-medium text-gray-900">Cost Optimized</p>
              <p className="text-sm text-gray-600">Route to PSP with lowest transaction fees</p>
            </div>
            <input
              type="radio"
              name="strategy"
              value="cost"
              checked={strategy === 'cost'}
              onChange={(e) => setStrategy(e.target.value)}
              className="w-4 h-4"
            />
          </label>
          <label className="flex items-center justify-between p-3 border rounded-lg cursor-pointer hover:bg-gray-50">
            <div>
              <p className="font-medium text-gray-900">Success Rate Priority</p>
              <p className="text-sm text-gray-600">Always route to PSP with highest success rate</p>
            </div>
            <input
              type="radio"
              name="strategy"
              value="success"
              checked={strategy === 'success'}
              onChange={(e) => setStrategy(e.target.value)}
              className="w-4 h-4"
            />
          </label>
        </div>
      </div>

      {/* Primary & Fallback Configuration */}
      <div className="bg-white rounded-lg shadow p-6">
        <h2 className="text-lg font-semibold text-gray-900 mb-4">Primary & Fallback Chain</h2>
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">Primary PSP</label>
            <select
              value={primaryPSP}
              onChange={(e) => setPrimaryPSP(e.target.value)}
              className="w-full px-3 py-2 border rounded-lg"
            >
              <option value="">Select Primary PSP</option>
              {activePSPs.map((psp) => (
                <option key={psp.id} value={psp.id}>{psp.name}</option>
              ))}
            </select>
          </div>

          {/* Visual Flow */}
          {primaryPSP && (
            <div className="p-4 bg-gray-50 rounded-lg">
              <p className="text-sm font-medium text-gray-700 mb-3">Payment Flow:</p>
              <div className="flex items-center space-x-2 flex-wrap">
                <div className="px-4 py-2 bg-blue-600 text-white rounded-lg font-medium">
                  {activePSPs.find(p => p.id === primaryPSP)?.name || 'Primary'}
                </div>
                {fallbackChain.map((pspId, index) => (
                  <div key={index} className="flex items-center space-x-2">
                    <ArrowRight className="w-4 h-4 text-gray-400" />
                    <div className="px-4 py-2 bg-yellow-100 text-yellow-800 rounded-lg font-medium">
                      {activePSPs.find(p => p.id === pspId)?.name || `Fallback ${index + 1}`}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">Fallback PSPs (in order)</label>
            <p className="text-xs text-gray-500 mb-2">If primary fails, automatically retry with these PSPs in order</p>
            {activePSPs.filter(p => p.id !== primaryPSP).map((psp, index) => (
              <label key={psp.id} className="flex items-center space-x-2 p-2 hover:bg-gray-50 rounded">
                <input
                  type="checkbox"
                  checked={fallbackChain.includes(psp.id)}
                  onChange={(e) => {
                    if (e.target.checked) {
                      setFallbackChain([...fallbackChain, psp.id]);
                    } else {
                      setFallbackChain(fallbackChain.filter(id => id !== psp.id));
                    }
                  }}
                  className="rounded"
                />
                <span className="text-sm">{psp.name}</span>
              </label>
            ))}
          </div>
        </div>
      </div>

      {/* Geographic Routing Rules */}
      <div className="bg-white rounded-lg shadow p-6">
        <div className="flex items-center space-x-3 mb-4">
          <Globe className="w-5 h-5 text-blue-600" />
          <h2 className="text-lg font-semibold text-gray-900">Geographic Routing Rules</h2>
        </div>
        <div className="space-y-3">
          {geoRules.map((rule, index) => (
            <div key={index} className="flex items-center justify-between p-3 border rounded-lg">
              <div className="flex items-center space-x-3">
                <Globe className="w-4 h-4 text-gray-400" />
                <span className="text-sm text-gray-900">{rule.region} Cards</span>
              </div>
              <div className="flex items-center space-x-2">
                <ArrowRight className="w-4 h-4 text-gray-400" />
                <select
                  value={rule.psp}
                  className="px-3 py-1 border rounded text-sm"
                  onChange={(e) => {
                    const newRules = [...geoRules];
                    newRules[index].psp = e.target.value;
                    setGeoRules(newRules);
                  }}
                >
                  {activePSPs.map((psp) => (
                    <option key={psp.id} value={psp.id.toLowerCase()}>{psp.name}</option>
                  ))}
                </select>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Benefits Info */}
      <div className="bg-green-50 border border-green-200 rounded-lg p-4">
        <div className="flex items-center space-x-3">
          <TrendingUp className="w-5 h-5 text-green-600" />
          <div className="text-sm text-green-900">
            <p className="font-medium mb-1">Expected Improvement</p>
            <p>Multi-PSP routing can improve your payment success rate by 2-3% on average.</p>
          </div>
        </div>
      </div>

      {/* Save Button */}
      <div className="flex justify-end">
        <button
          onClick={handleSaveRouting}
          className="flex items-center space-x-2 px-6 py-3 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
        >
          <Save className="w-5 h-5" />
          <span>Save Routing Configuration</span>
        </button>
      </div>
    </div>
  );
}


