import { Merchant } from '../lib/api';
import { Users, Zap, Clock, CheckCircle } from 'lucide-react';

interface StatsCardsProps {
  merchants: Merchant[];
  loading: boolean;
}

export default function StatsCards({ merchants, loading }: StatsCardsProps) {
  const stats = {
    total: merchants.length,
    autoApproved: merchants.filter(m => m.auto_approved).length,
    pending: merchants.filter(m => m.status === 'pending_verification').length,
    approved: merchants.filter(m => m.status === 'approved').length,
  };

  const cards = [
    {
      title: 'Total Merchants',
      value: stats.total,
      icon: Users,
      color: 'bg-blue-500',
      textColor: 'text-blue-600',
      bgColor: 'bg-blue-50',
    },
    {
      title: 'Auto Approved',
      value: stats.autoApproved,
      icon: Zap,
      color: 'bg-green-500',
      textColor: 'text-green-600',
      bgColor: 'bg-green-50',
    },
    {
      title: 'Pending Review',
      value: stats.pending,
      icon: Clock,
      color: 'bg-yellow-500',
      textColor: 'text-yellow-600',
      bgColor: 'bg-yellow-50',
    },
    {
      title: 'Approved',
      value: stats.approved,
      icon: CheckCircle,
      color: 'bg-emerald-500',
      textColor: 'text-emerald-600',
      bgColor: 'bg-emerald-50',
    },
  ];

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
      {cards.map((card) => {
        const Icon = card.icon;
        return (
          <div
            key={card.title}
            className="bg-white rounded-xl shadow-sm border border-slate-200 p-6 hover:shadow-md transition-shadow"
          >
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-slate-600">{card.title}</p>
                <p className="text-3xl font-bold text-slate-900 mt-2">
                  {loading ? '...' : card.value}
                </p>
              </div>
              <div className={`w-12 h-12 ${card.bgColor} rounded-lg flex items-center justify-center`}>
                <Icon className={`w-6 h-6 ${card.textColor}`} />
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}


