import { useState, useEffect } from 'react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Users, Zap, Clock, RefreshCw, ExternalLink, Search } from 'lucide-react';
import { useToast } from '@/components/ui/use-toast';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'https://web-production-fedb.up.railway.app';

export default function MerchantDashboard() {
  const [merchants, setMerchants] = useState([]);
  const [filteredMerchants, setFilteredMerchants] = useState([]);
  const [adminToken, setAdminToken] = useState(localStorage.getItem('pivota_admin_jwt') || '');
  const [statusFilter, setStatusFilter] = useState('all');
  const [searchText, setSearchText] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const { toast } = useToast();

  // Fetch merchants
  const fetchMerchants = async () => {
    if (!adminToken) {
      toast({
        title: "Authentication Required",
        description: "Please enter your Admin JWT token",
        variant: "destructive"
      });
      return;
    }

    setIsLoading(true);
    try {
      const response = await fetch(`${API_BASE_URL}/merchant/onboarding/all`, {
        headers: {
          'Authorization': `Bearer ${adminToken}`,
          'Content-Type': 'application/json'
        }
      });

      if (!response.ok) {
        throw new Error('Failed to fetch merchants');
      }

      const data = await response.json();
      setMerchants(data.merchants || []);
      toast({
        title: "Success",
        description: `Loaded ${data.merchants?.length || 0} merchants`
      });
    } catch (error) {
      toast({
        title: "Error",
        description: error.message,
        variant: "destructive"
      });
    } finally {
      setIsLoading(false);
    }
  };

  // Filter merchants
  useEffect(() => {
    let filtered = merchants;

    // Status filter
    if (statusFilter !== 'all') {
      filtered = filtered.filter(m => m.status === statusFilter);
    }

    // Search filter
    if (searchText) {
      const search = searchText.toLowerCase();
      filtered = filtered.filter(m =>
        m.business_name?.toLowerCase().includes(search) ||
        m.contact_email?.toLowerCase().includes(search) ||
        m.merchant_id?.toLowerCase().includes(search)
      );
    }

    setFilteredMerchants(filtered);
  }, [merchants, statusFilter, searchText]);

  // Save token to localStorage
  const handleTokenChange = (value: string) => {
    setAdminToken(value);
    localStorage.setItem('pivota_admin_jwt', value);
  };

  // Stats
  const totalMerchants = merchants.length;
  const autoApproved = merchants.filter(m => m.auto_approved).length;
  const pendingReview = merchants.filter(m => m.status === 'pending_verification').length;

  return (
    <div className="container mx-auto p-6 space-y-6">
      {/* Header */}
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-3xl font-bold">Merchant Management</h1>
          <p className="text-gray-500">Unified onboarding and merchant dashboard</p>
        </div>
        <Button variant="outline" onClick={() => window.open(`${API_BASE_URL}/merchant/onboarding/portal`, '_blank')}>
          <ExternalLink className="mr-2 h-4 w-4" />
          Merchant Portal
        </Button>
      </div>

      {/* Stats Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Total Merchants</CardTitle>
            <Users className="h-4 w-4 text-blue-600" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{totalMerchants}</div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Auto Approved</CardTitle>
            <Zap className="h-4 w-4 text-green-600" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{autoApproved}</div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Pending Review</CardTitle>
            <Clock className="h-4 w-4 text-yellow-600" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{pendingReview}</div>
          </CardContent>
        </Card>
      </div>

      {/* Toolbar */}
      <Card>
        <CardContent className="pt-6">
          <div className="flex flex-wrap gap-4">
            <Input
              placeholder="Admin JWT Token..."
              value={adminToken}
              onChange={(e) => handleTokenChange(e.target.value)}
              className="flex-1 min-w-[300px]"
              type="password"
            />
            
            <Select value={statusFilter} onValueChange={setStatusFilter}>
              <SelectTrigger className="w-[180px]">
                <SelectValue placeholder="Filter by status" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All Status</SelectItem>
                <SelectItem value="approved">Approved</SelectItem>
                <SelectItem value="pending_verification">Pending</SelectItem>
                <SelectItem value="rejected">Rejected</SelectItem>
              </SelectContent>
            </Select>

            <div className="relative flex-1 min-w-[200px]">
              <Search className="absolute left-3 top-3 h-4 w-4 text-gray-400" />
              <Input
                placeholder="Search merchants..."
                value={searchText}
                onChange={(e) => setSearchText(e.target.value)}
                className="pl-9"
              />
            </div>

            <Button onClick={fetchMerchants} disabled={isLoading}>
              <RefreshCw className={`mr-2 h-4 w-4 ${isLoading ? 'animate-spin' : ''}`} />
              Refresh
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* Merchants Table */}
      <Card>
        <CardHeader>
          <CardTitle>Merchants</CardTitle>
          <CardDescription>
            Showing {filteredMerchants.length} of {totalMerchants} merchants
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Merchant ID</TableHead>
                <TableHead>Business Name</TableHead>
                <TableHead>Store URL</TableHead>
                <TableHead>Email</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Auto</TableHead>
                <TableHead>Confidence</TableHead>
                <TableHead>Created</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filteredMerchants.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={8} className="text-center text-gray-500">
                    {isLoading ? 'Loading...' : 'No merchants found. Click Refresh to load data.'}
                  </TableCell>
                </TableRow>
              ) : (
                filteredMerchants.map((merchant) => (
                  <TableRow key={merchant.merchant_id}>
                    <TableCell className="font-mono text-sm">{merchant.merchant_id}</TableCell>
                    <TableCell className="font-medium">{merchant.business_name}</TableCell>
                    <TableCell>
                      <a 
                        href={merchant.store_url} 
                        target="_blank" 
                        rel="noopener noreferrer"
                        className="text-blue-600 hover:underline text-sm"
                      >
                        {merchant.store_url}
                      </a>
                    </TableCell>
                    <TableCell className="text-sm">{merchant.contact_email}</TableCell>
                    <TableCell>
                      <Badge variant={
                        merchant.status === 'approved' ? 'default' :
                        merchant.status === 'pending_verification' ? 'secondary' :
                        'destructive'
                      }>
                        {merchant.status}
                      </Badge>
                    </TableCell>
                    <TableCell>{merchant.auto_approved ? '✓' : '-'}</TableCell>
                    <TableCell>
                      {merchant.approval_confidence 
                        ? `${(merchant.approval_confidence * 100).toFixed(0)}%` 
                        : '-'}
                    </TableCell>
                    <TableCell className="text-sm">
                      {new Date(merchant.created_at).toLocaleDateString()}
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}

