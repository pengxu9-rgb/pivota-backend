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

interface FileWithType {
  file: File;
  documentType: string;
}

export const DocumentUploadModal: React.FC<DocumentUploadModalProps> = ({
  isOpen,
  onClose,
  merchantId,
  merchantName,
  onUpload
}) => {
  const [selectedFiles, setSelectedFiles] = useState<FileWithType[]>([]);
  const [isUploading, setIsUploading] = useState(false);
  const [error, setError] = useState('');
  const [uploadProgress, setUploadProgress] = useState<string>('');

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      const newFiles: FileWithType[] = Array.from(e.target.files).map(file => ({
        file,
        documentType: 'other' // Default type, can be changed
      }));
      setSelectedFiles([...selectedFiles, ...newFiles]);
      setError('');
    }
  };

  const handleRemoveFile = (index: number) => {
    setSelectedFiles(selectedFiles.filter((_, i) => i !== index));
  };

  const handleChangeDocumentType = (index: number, type: string) => {
    const updated = [...selectedFiles];
    updated[index].documentType = type;
    setSelectedFiles(updated);
  };

  const handleUploadAll = async () => {
    if (selectedFiles.length === 0) {
      setError('Please select at least one file to upload');
      return;
    }

    setIsUploading(true);
    setError('');

    try {
      let successCount = 0;
      
      for (let i = 0; i < selectedFiles.length; i++) {
        const { file, documentType } = selectedFiles[i];
        setUploadProgress(`Uploading ${i + 1} of ${selectedFiles.length}: ${file.name}...`);
        
        await onUpload(merchantId, documentType, file);
        successCount++;
      }
      
      // Reset and close
      setSelectedFiles([]);
      setUploadProgress('');
      alert(`✅ Successfully uploaded ${successCount} document(s)!`);
      onClose();
    } catch (err: any) {
      setError(err.message || 'Failed to upload documents');
      setUploadProgress('');
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

          {uploadProgress && (
            <div className="bg-blue-50 border border-blue-200 text-blue-700 px-4 py-3 rounded">
              {uploadProgress}
            </div>
          )}

          {/* File Upload */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Select Files (Multiple) *
            </label>
            <div className="border-2 border-dashed border-gray-300 rounded-lg p-6 text-center">
              <input
                type="file"
                onChange={handleFileSelect}
                className="hidden"
                id="file-upload"
                accept=".pdf,.jpg,.jpeg,.png"
                multiple
              />
              <label
                htmlFor="file-upload"
                className="cursor-pointer flex flex-col items-center"
              >
                <Upload size={48} className="text-gray-400 mb-2" />
                <span className="text-sm text-gray-600">
                  Click to select files (or select multiple)
                </span>
                <span className="text-xs text-gray-500 mt-1">
                  PDF, JPG, PNG (Max 10MB each)
                </span>
              </label>
            </div>
          </div>

          {/* Selected Files List */}
          {selectedFiles.length > 0 && (
            <div className="space-y-3">
              <h3 className="text-sm font-medium text-gray-900">
                Selected Files ({selectedFiles.length})
              </h3>
              {selectedFiles.map((item, index) => (
                <div key={index} className="bg-gray-50 border border-gray-200 rounded-lg p-3">
                  <div className="flex items-start gap-3">
                    <FileText className="text-blue-600 mt-1" size={20} />
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium text-gray-900 truncate">
                        {item.file.name}
                      </p>
                      <p className="text-xs text-gray-500">
                        {(item.file.size / 1024).toFixed(1)} KB
                      </p>
                      <select
                        value={item.documentType}
                        onChange={(e) => handleChangeDocumentType(index, e.target.value)}
                        className="mt-2 text-xs px-2 py-1 border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-blue-500"
                      >
                        {DOCUMENT_TYPES.map(type => (
                          <option key={type.value} value={type.value}>
                            {type.label}
                          </option>
                        ))}
                      </select>
                    </div>
                    <button
                      onClick={() => handleRemoveFile(index)}
                      className="text-red-500 hover:text-red-700"
                    >
                      <X size={18} />
                    </button>
                  </div>
                </div>
              ))}
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
              onClick={handleUploadAll}
              className="btn btn-primary"
              disabled={isUploading || selectedFiles.length === 0}
            >
              {isUploading ? 'Uploading...' : `Upload ${selectedFiles.length} Document(s)`}
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

