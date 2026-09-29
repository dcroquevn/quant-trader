import { useQuery } from '@tanstack/react-query';
import clsx from 'clsx';
import {
  Activity,
  AlertTriangle,
  Briefcase,
  Database,
  FlaskConical,
  List,
  Radar,
  ShieldCheck,
  SlidersHorizontal,
} from 'lucide-react';
import { NavLink, Navigate, Route, Routes } from 'react-router-dom';

import { Badge } from './components/ui';
import { api } from './lib/api';
import AssetPage from './pages/AssetPage';
import Backtest from './pages/Backtest';
import { Optimization, Portfolio } from './pages/ComingLater';
import DataHealth from './pages/DataHealth';
import Limitations from './pages/Limitations';
import Overview from './pages/Overview';
import Scanner from './pages/Scanner';
import UniversePage from './pages/UniversePage';

const NAV = [
  { to: '/overview', label: 'Overview', icon: Activity },
  { to: '/scanner', label: 'Scanner', icon: Radar },
  { to: '/backtest', label: 'Backtest', icon: FlaskConical },
  { to: '/universe', label: 'Universe', icon: List },
  { to: '/portfolio', label: 'Portfolio', icon: Briefcase, phase: 6 },
  { to: '/optimization', label: 'Optimization', icon: SlidersHorizontal, phase: 4 },
  { to: '/data-health', label: 'Data health', icon: Database },
  { to: '/limitations', label: 'Limitations', icon: AlertTriangle },
];

function Header() {
  const { data: health } = useQuery({ queryKey: ['health'], queryFn: api.health });

  return (
    <header className="sticky top-0 z-30 border-b border-terminal-750 bg-terminal-900/95 backdrop-blur">
      <div className="mx-auto flex max-w-[1600px] flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
        <div className="flex items-center gap-2.5">
          <div className="grid h-7 w-7 place-items-center rounded bg-accent/15 ring-1 ring-inset ring-accent/30">
            <Activity className="h-4 w-4 text-accent" />
          </div>
          <div>
            <h1 className="text-sm font-bold tracking-tight text-slate-100">QUANT TRADER</h1>
            <p className="text-2xs text-slate-500">US &amp; Chile equity research</p>
          </div>
        </div>

        <nav className="flex flex-1 flex-wrap items-center gap-1">
          {NAV.map(({ to, label, icon: Icon, phase }) => (
            <NavLink
              key={to}
              to={to}
              title={phase ? `Scheduled for Phase ${phase}` : undefined}
              className={({ isActive }) =>
                clsx(
                  'inline-flex items-center gap-1.5 rounded px-2.5 py-1.5 text-xs font-medium transition',
                  isActive
                    ? 'bg-terminal-750 text-slate-100'
                    : 'text-slate-400 hover:bg-terminal-800 hover:text-slate-200',
                  // Routes whose engine does not exist yet are visibly recessive, so the
                  // nav does not promise more than the app can do.
                  phase && !isActive && 'text-slate-600',
                )
              }
            >
              <Icon className="h-3.5 w-3.5" />
              {label}
              {phase && (
                <span className="text-[9px] font-normal text-slate-700">P{phase}</span>
              )}
            </NavLink>
          ))}
        </nav>

        <div className="flex items-center gap-2">
          {/* Trading mode is always on screen. It is the one piece of state where a
              wrong assumption costs real money. */}
          <Badge tone="gain" title="Simulated fills only. No order leaves this machine.">
            <ShieldCheck className="mr-1 h-3 w-3" />
            PAPER
          </Badge>
          <Badge tone="neutral" title="Live order routing is not implemented in this build.">
            LIVE: OFF
          </Badge>
          {health && <span className="text-2xs text-slate-600">v{health.version}</span>}
        </div>
      </div>
    </header>
  );
}

export default function App() {
  return (
    <div className="flex min-h-full flex-col">
      <Header />
      <main className="mx-auto w-full max-w-[1600px] flex-1 px-4 py-5">
        <Routes>
          <Route path="/" element={<Navigate to="/overview" replace />} />
          <Route path="/overview" element={<Overview />} />
          <Route path="/scanner" element={<Scanner />} />
          <Route path="/backtest" element={<Backtest />} />
          <Route path="/universe" element={<UniversePage />} />
          <Route path="/portfolio" element={<Portfolio />} />
          <Route path="/optimization" element={<Optimization />} />
          <Route path="/asset/:symbol" element={<AssetPage />} />
          <Route path="/data-health" element={<DataHealth />} />
          <Route path="/limitations" element={<Limitations />} />
          <Route
            path="*"
            element={
              <div className="py-20 text-center text-sm text-slate-500">
                Not found. <NavLink to="/overview" className="text-accent hover:underline">Go to overview</NavLink>
              </div>
            }
          />
        </Routes>
      </main>
      <footer className="border-t border-terminal-800 px-4 py-4">
        <p className="mx-auto max-w-[1600px] text-2xs leading-relaxed text-slate-600">
          Every figure shown describes <strong className="text-slate-500">past</strong> price
          behaviour. Nothing here is a forecast, and no statistic implies a probability of any
          future outcome. Phase 1 of 8: data foundation, database and indicators. Strategy,
          backtesting, optimisation and paper trading are not implemented yet.
        </p>
      </footer>
    </div>
  );
}
