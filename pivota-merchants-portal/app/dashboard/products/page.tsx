'use client';

import { useState, useEffect } from 'react';
import { Package, Plus, Search, RefreshCw } from 'lucide-react';
import { apiClient } from '@/lib/api-client';

export default function ProductsPage() {
  const [products, setProducts] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [searchTerm, setSearchTerm] = useState('');
  const [merchantId, setMerchantId] = useState('');
  const [showAddModal, setShowAddModal] = useState(false);
  const [selectedProduct, setSelectedProduct] = useState<any>(null);
  const [showEditModal, setShowEditModal] = useState(false);
  const [showViewModal, setShowViewModal] = useState(false);

  useEffect(() => {
    const id = localStorage.getItem('merchant_id') || '';
    setMerchantId(id);
    loadProducts();
  }, []);

  const loadProducts = async () => {
    try {
      setLoading(true);
      const data = await apiClient.getProducts();
      setProducts(data);
    } catch (error) {
      console.error('Failed to load products:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleSync = async () => {
    setSyncing(true);
    try {
      const result = await apiClient.syncShopifyProducts();
      alert(result.message || '✅ Products synced successfully!');
      await loadProducts();
    } catch (error: any) {
      alert('❌ Failed to sync: ' + (error.response?.data?.detail || error.message));
    } finally {
      setSyncing(false);
    }
  };

  const handleAddProduct = () => {
    setShowAddModal(true);
    // In a real implementation, this would open a modal with a form
    alert('Add Product feature: Coming soon! This will open a modal to add a new product.');
  };

  const formatCurrency = (amount: number) => {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
    }).format(amount);
  };

  const filteredProducts = products.filter(product =>
    searchTerm === '' || 
    product.name?.toLowerCase().includes(searchTerm.toLowerCase()) ||
    product.sku?.toLowerCase().includes(searchTerm.toLowerCase())
  );

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Products</h1>
          <p className="text-gray-600">Manage your product catalog</p>
        </div>
        <div className="flex items-center space-x-2">
          <button
            onClick={handleSync}
            disabled={syncing}
            className="flex items-center space-x-2 px-4 py-2 border rounded-lg hover:bg-gray-50 disabled:opacity-50"
          >
            <RefreshCw className={`w-4 h-4 ${syncing ? 'animate-spin' : ''}`} />
            <span>Sync from Store</span>
          </button>
          <button 
            onClick={handleAddProduct}
            className="flex items-center space-x-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
          >
            <Plus className="w-4 h-4" />
            <span>Add Product</span>
          </button>
        </div>
      </div>

      <div className="bg-white rounded-lg shadow">
        <div className="p-4 border-b">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-gray-400" />
            <input
              type="text"
              placeholder="Search products..."
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="w-full pl-10 pr-4 py-2 border rounded-lg"
            />
          </div>
        </div>

        <div className="p-6">
          {filteredProducts.length > 0 ? (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-6">
              {filteredProducts.map((product) => (
                <div key={product.id} className="border rounded-lg p-4 hover:shadow-lg transition-shadow">
                  <div className="w-full h-32 bg-gray-100 rounded-lg mb-4 overflow-hidden">
                    {product.image_url || product.images?.[0] ? (
                      <img 
                        src={product.image_url || product.images[0]} 
                        alt={product.title || product.name}
                        className="w-full h-full object-cover"
                        onError={(e) => {
                          e.currentTarget.style.display = 'none';
                          e.currentTarget.parentElement!.innerHTML = '<div class="w-full h-full flex items-center justify-center"><svg class="w-12 h-12 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"></path></svg></div>';
                        }}
                      />
                    ) : (
                      <div className="w-full h-full flex items-center justify-center">
                        <Package className="w-12 h-12 text-gray-400" />
                      </div>
                    )}
                  </div>
                  <h3 className="font-medium text-gray-900 truncate">{product.title || product.name}</h3>
                  <p className="text-sm text-gray-600 mb-2">{product.sku || 'No SKU'}</p>
                  <div className="flex items-center justify-between">
                    <span className="text-lg font-bold text-gray-900">
                      {formatCurrency(product.price || 0)}
                    </span>
                    <span className={`text-sm ${(product.inventory_quantity || product.stock || 0) > 0 ? 'text-green-600' : 'text-red-600'}`}>
                      Stock: {product.inventory_quantity ?? product.stock ?? 0}
                    </span>
                  </div>
                  <div className="mt-4 flex items-center space-x-2">
                    <button 
                      onClick={() => {
                        setSelectedProduct(product);
                        setShowEditModal(true);
                      }}
                      className="flex-1 px-3 py-1 border rounded text-sm hover:bg-gray-50"
                    >
                      Edit
                    </button>
                    <button 
                      onClick={() => {
                        setSelectedProduct(product);
                        setShowViewModal(true);
                      }}
                      className="flex-1 px-3 py-1 border rounded text-sm hover:bg-gray-50"
                    >
                      View
                    </button>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-center py-12">
              <Package className="w-12 h-12 text-gray-400 mx-auto mb-3" />
              <p className="text-gray-600 mb-4">No products found</p>
              <button
                onClick={handleSync}
                className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
              >
                Sync Products from Store
              </button>
            </div>
          )}
        </div>
      </div>

      {/* View Product Modal */}
      {showViewModal && selectedProduct && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50" onClick={() => setShowViewModal(false)}>
          <div className="bg-white rounded-lg p-6 max-w-2xl w-full m-4 max-h-[80vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
            <h3 className="text-xl font-semibold mb-4">Product Details</h3>
            
            {selectedProduct.image_url || selectedProduct.images?.[0] ? (
              <img 
                src={selectedProduct.image_url || selectedProduct.images[0]} 
                alt={selectedProduct.title || selectedProduct.name}
                className="w-full h-64 object-cover rounded-lg mb-4"
              />
            ) : null}
            
            <div className="space-y-3">
              <div>
                <label className="text-sm font-medium text-gray-600">Product Name</label>
                <p className="text-gray-900">{selectedProduct.title || selectedProduct.name}</p>
              </div>
              <div>
                <label className="text-sm font-medium text-gray-600">SKU</label>
                <p className="text-gray-900">{selectedProduct.sku || 'N/A'}</p>
              </div>
              <div>
                <label className="text-sm font-medium text-gray-600">Price</label>
                <p className="text-gray-900">{formatCurrency(selectedProduct.price || 0)}</p>
              </div>
              <div>
                <label className="text-sm font-medium text-gray-600">Stock</label>
                <p className="text-gray-900">{selectedProduct.inventory_quantity ?? selectedProduct.stock ?? 0}</p>
              </div>
              <div>
                <label className="text-sm font-medium text-gray-600">Description</label>
                <p className="text-gray-900">{selectedProduct.description || 'No description'}</p>
              </div>
              {selectedProduct.variants && selectedProduct.variants.length > 0 && (
                <div>
                  <label className="text-sm font-medium text-gray-600">Variants</label>
                  <div className="mt-2 space-y-1">
                    {selectedProduct.variants.map((v: any, i: number) => (
                      <p key={i} className="text-sm text-gray-700">
                        {v.title}: {formatCurrency(v.price)} (Stock: {v.inventory_quantity || 0})
                      </p>
                    ))}
                  </div>
                </div>
              )}
            </div>
            
            <button
              onClick={() => setShowViewModal(false)}
              className="w-full mt-6 py-2 border rounded-lg hover:bg-gray-50"
            >
              Close
            </button>
          </div>
        </div>
      )}

      {/* Edit Product Modal */}
      {showEditModal && selectedProduct && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50" onClick={() => setShowEditModal(false)}>
          <div className="bg-white rounded-lg p-6 max-w-2xl w-full m-4" onClick={(e) => e.stopPropagation()}>
            <h3 className="text-xl font-semibold mb-4">Edit Product</h3>
            
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Product Name</label>
                <input 
                  type="text"
                  defaultValue={selectedProduct.title || selectedProduct.name}
                  className="w-full px-3 py-2 border rounded-lg"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Price</label>
                <input 
                  type="number"
                  step="0.01"
                  defaultValue={selectedProduct.price || 0}
                  className="w-full px-3 py-2 border rounded-lg"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Stock</label>
                <input 
                  type="number"
                  defaultValue={selectedProduct.inventory_quantity ?? selectedProduct.stock ?? 0}
                  className="w-full px-3 py-2 border rounded-lg"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Description</label>
                <textarea 
                  defaultValue={selectedProduct.description || ''}
                  rows={4}
                  className="w-full px-3 py-2 border rounded-lg"
                />
              </div>
            </div>
            
            <div className="mt-6 flex items-center space-x-3">
              <button
                onClick={() => {
                  alert('✅ Product update feature coming soon!');
                  setShowEditModal(false);
                }}
                className="flex-1 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
              >
                Save Changes
              </button>
              <button
                onClick={() => setShowEditModal(false)}
                className="flex-1 py-2 border rounded-lg hover:bg-gray-50"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

