import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';

import App from './App';
import './index.css';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Bars change once a day at most, so refetching on every focus is pure noise.
      staleTime: 5 * 60 * 1000,
      refetchOnWindowFocus: false,
      // One retry only. A missing symbol returns 404 and retrying cannot help;
      // hammering a free provider's backend is how you get rate-limited.
      retry: 1,
    },
  },
});

const root = document.getElementById('root');
if (!root) throw new Error('Missing #root element');

ReactDOM.createRoot(root).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
