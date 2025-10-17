import { useState } from 'react';
import { CheckCircle, Loader, Store, CreditCard, FileText, Rocket } from 'lucide-react';
import RegistrationStep from './components/RegistrationStep';
import PSPSetupStep from './components/PSPSetupStep';
import DocumentUploadStep from './components/DocumentUploadStep';
import CompletionStep from './components/CompletionStep';

type Step = 'register' | 'psp' | 'documents' | 'complete';

interface OnboardingData {
  merchant_id?: string;
  business_name?: string;
  auto_approved?: boolean;
  confidence_score?: number;
  message?: string;
  api_key?: string;
}

function App() {
  const [currentStep, setCurrentStep] = useState<Step>('register');
  const [onboardingData, setOnboardingData] = useState<OnboardingData>({});

  const steps = [
    { id: 'register', title: 'Business Info', icon: Store },
    { id: 'psp', title: 'Payment Setup', icon: CreditCard },
    { id: 'documents', title: 'Documents', icon: FileText },
    { id: 'complete', title: 'Complete', icon: Rocket },
  ];

  const currentStepIndex = steps.findIndex((s) => s.id === currentStep);

  const handleRegistrationComplete = (data: OnboardingData) => {
    setOnboardingData({ ...onboardingData, ...data });
    setCurrentStep('psp');
  };

  const handlePSPComplete = (data: OnboardingData) => {
    setOnboardingData({ ...onboardingData, ...data });
    setCurrentStep('documents');
  };

  const handleDocumentsComplete = () => {
    setCurrentStep('complete');
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-blue-50 via-indigo-50 to-purple-50">
      {/* Header */}
      <header className="bg-white border-b border-slate-200 shadow-sm">
        <div className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
          <div className="flex items-center gap-3">
            <div className="w-12 h-12 bg-gradient-to-br from-blue-500 to-indigo-600 rounded-xl flex items-center justify-center">
              <Store className="w-7 h-7 text-white" />
            </div>
            <div>
              <h1 className="text-2xl font-bold text-slate-900">Pivota Merchant Onboarding</h1>
              <p className="text-sm text-slate-600">Start accepting payments in minutes</p>
            </div>
          </div>
        </div>
      </header>

      {/* Progress Steps */}
      <div className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-8 mb-8">
          <div className="flex items-center justify-between">
            {steps.map((step, index) => {
              const Icon = step.icon;
              const isCompleted = index < currentStepIndex;
              const isCurrent = index === currentStepIndex;
              const isUpcoming = index > currentStepIndex;

              return (
                <div key={step.id} className="flex items-center flex-1">
                  <div className="flex flex-col items-center flex-1">
                    <div
                      className={`w-12 h-12 rounded-full flex items-center justify-center transition-all ${
                        isCompleted
                          ? 'bg-green-500 text-white'
                          : isCurrent
                          ? 'bg-blue-500 text-white ring-4 ring-blue-100'
                          : 'bg-slate-200 text-slate-400'
                      }`}
                    >
                      {isCompleted ? <CheckCircle className="w-6 h-6" /> : <Icon className="w-6 h-6" />}
                    </div>
                    <div className="mt-2 text-center">
                      <p
                        className={`text-sm font-medium ${
                          isCurrent ? 'text-blue-600' : isCompleted ? 'text-green-600' : 'text-slate-500'
                        }`}
                      >
                        {step.title}
                      </p>
                    </div>
                  </div>
                  {index < steps.length - 1 && (
                    <div
                      className={`h-1 w-full mx-4 transition-all ${
                        isCompleted ? 'bg-green-500' : 'bg-slate-200'
                      }`}
                    />
                  )}
                </div>
              );
            })}
          </div>
        </div>

        {/* Step Content */}
        <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-8">
          {currentStep === 'register' && <RegistrationStep onComplete={handleRegistrationComplete} />}
          {currentStep === 'psp' && (
            <PSPSetupStep merchantId={onboardingData.merchant_id!} onComplete={handlePSPComplete} />
          )}
          {currentStep === 'documents' && (
            <DocumentUploadStep
              merchantId={onboardingData.merchant_id!}
              onComplete={handleDocumentsComplete}
              autoApproved={onboardingData.auto_approved}
            />
          )}
          {currentStep === 'complete' && <CompletionStep data={onboardingData} />}
        </div>
      </div>

      {/* Footer */}
      <footer className="mt-12 py-6 text-center text-sm text-slate-500">
        <p>Pivota Merchant Onboarding • Secure & Fast Payment Processing</p>
      </footer>
    </div>
  );
}

export default App;


