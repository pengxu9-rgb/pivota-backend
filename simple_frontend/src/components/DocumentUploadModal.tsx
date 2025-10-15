import React, { useState } from 'react';
import { X, Upload, FileText } from 'lucide-react';

interface DocumentUploadModalProps {
  isOpen: boolean;
  onClose: () => void;
  merchantId: number;
  merchantName: string;
  onUpload: (merchantId: number, documentType: string, file: File) => Promise<void>;
}

const DOCUMENT_TYPES = [
  { value: 'business_license', label: 'Business License' },
  { value: 'tax_id', label: 'Tax ID Document' },
  { value: 'bank_statement', label: 'Bank Statement' },
  { value: 'proof_of_address', label: 'Proof of Address' },
  { value: 'identity_proof', label: 'Owner ID/Passport' },
  { value: 'other', label: 'Other Document' }
];

export const DocumentUploadModal: React.FC<DocumentUploadModalProps> = ({
  isOpen,
  onClose,
  merchantId,
  merchantName,
  onUpload
}) => {
  const [documentType, setDocumentType] = useState('business_license');
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [error, setError] = useState('');

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      setSelectedFile(e.target.files[0]);
      setError('');
    }
  };

  const handleUpload = async () => {
    if (!selectedFile) {
      setError('Please select a file to upload');
      return;
    }

    setIsUploading(true);
    setError('');

    try {
      await onUpload(merchantId, documentType, selectedFile);
      
      // Reset and close
      setSelectedFile(null);
      setDocumentType('business_license');
      alert(`✅ Document uploaded successfully!\n\nFile: ${selectedFile.name}\nType: ${DOCUMENT_TYPES.find(t => t.value === documentType)?.label}`);
      onClose();
    } catch (err: any) {
      setError(err.message || 'Failed to upload document');
    } finally {
      setIsUploading(false);
    }
  };

  if (!isOpen) return null;

  return (
    <div 
      className="modal-backdrop"
      onClick={onClose}
    >
      <div 
        className="bg-white rounded-lg w-full overflow-y-auto"
        style={{ 
          zIndex: 1000000,
          maxWidth: '600px',
          maxHeight: '85vh'
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sticky top-0 bg-white border-b border-gray-200 p-6 flex justify-between items-center">
          <div>
            <h2 className="text-2xl font-bold">Upload KYB Document</h2>
            <p className="text-sm text-gray-600 mt-1">
              Merchant: {merchantName} (ID: {merchantId})
            </p>
          </div>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-700">
            <X size={24} />
          </button>
        </div>

        <div className="p-6 space-y-6">
          {error && (
            <div className="bg-red-100 border border-red-400 text-red-700 px-4 py-3 rounded">
              {error}
            </div>
          )}

          {/* Document Type */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Document Type *
            </label>
            <select
              value={documentType}
              onChange={(e) => setDocumentType(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              {DOCUMENT_TYPES.map(type => (
                <option key={type.value} value={type.value}>
                  {type.label}
                </option>
              ))}
            </select>
          </div>

          {/* File Upload */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Select File *
            </label>
            <div className="border-2 border-dashed border-gray-300 rounded-lg p-6 text-center">
              <input
                type="file"
                onChange={handleFileSelect}
                className="hidden"
                id="file-upload"
                accept=".pdf,.jpg,.jpeg,.png"
              />
              <label
                htmlFor="file-upload"
                className="cursor-pointer flex flex-col items-center"
              >
                <Upload size={48} className="text-gray-400 mb-2" />
                <span className="text-sm text-gray-600">
                  {selectedFile ? selectedFile.name : 'Click to select a file'}
                </span>
                <span className="text-xs text-gray-500 mt-1">
                  PDF, JPG, PNG (Max 10MB)
                </span>
              </label>
            </div>
          </div>

          {selectedFile && (
            <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 flex items-center gap-3">
              <FileText className="text-blue-600" size={24} />
              <div className="flex-1">
                <p className="text-sm font-medium text-gray-900">{selectedFile.name}</p>
                <p className="text-xs text-gray-600">
                  {(selectedFile.size / 1024).toFixed(1)} KB
                </p>
              </div>
            </div>
          )}

          {/* Action Buttons */}
          <div className="flex justify-end gap-3 pt-4 border-t">
            <button
              onClick={onClose}
              className="btn btn-secondary"
              disabled={isUploading}
            >
              Cancel
            </button>
            <button
              onClick={handleUpload}
              className="btn btn-primary"
              disabled={isUploading || !selectedFile}
            >
              {isUploading ? 'Uploading...' : 'Upload Document'}
            </button>
          </div>

          {/* Instructions */}
          <div className="bg-gray-50 rounded-lg p-4">
            <p className="text-sm font-medium text-gray-900 mb-2">📋 Required Documents:</p>
            <ul className="text-xs text-gray-600 space-y-1">
              <li>• Business License or Registration</li>
              <li>• Tax ID Document</li>
              <li>• Recent Bank Statement</li>
              <li>• Proof of Address (Utility bill)</li>
              <li>• Owner's ID or Passport</li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
};

