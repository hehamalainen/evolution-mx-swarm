import { useState, useMemo } from 'react';
import { useEconomicFlowQuery, useAlignmentMatrixQuery, usePhaseTransitionQuery } from '@/lib/api_adapter';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip as RechartsTooltip, Legend, ResponsiveContainer, ScatterChart, Scatter, ZAxis } from 'recharts';
import { Activity, Zap, Compass, DollarSign, Users, Database } from 'lucide-react';

export function LensesPanel() {
  const [activeTab, setActiveTab] = useState<'economic' | 'alignment' | 'phase'>('economic');

  const econQuery = useEconomicFlowQuery();
  const alignQuery = useAlignmentMatrixQuery();
  const phaseQuery = usePhaseTransitionQuery();

  // Formatting for Economic Bubble Chart (Solar System)
  const econData = useMemo(() => {
    if (!econQuery.data) return [];
    return econQuery.data.map(d => ({
      ...d,
      // We arbitrary position them in a circle or randomly inside a boundary for a star system look
      // But for a scatter chart, we need x, y. Let's create an "orbit" layout.
      orbitX: (Math.random() - 0.5) * 100 * (1 / (d.economic_mentions + 1)),
      orbitY: (Math.random() - 0.5) * 100 * (1 / (d.economic_mentions + 1)),
    }));
  }, [econQuery.data]);

  // Formatting for Phase Transitions
  const phaseData = useMemo(() => {
    if (!phaseQuery.data) return [];
    const entries = Object.entries(phaseQuery.data).sort((a, b) => a[0].localeCompare(b[0]));
    return entries.map(([date, counts]) => ({
      date: date.substring(5), // MM-DD
      Structural: counts.Structural,
      Ritual: counts.Ritual,
    }));
  }, [phaseQuery.data]);

  const CustomTooltip = ({ active, payload, label }: any) => {
    if (active && payload && payload.length) {
      if (payload[0].payload.name) {
        // Scatter tooltips
        return (
          <div className="bg-black/80 border border-white/20 text-white p-3 rounded-md text-xs font-mono">
            <p className="font-bold text-sm mb-1">{payload[0].payload.name}</p>
            {payload.map((entry: any, index: number) => (
              <p key={index}>
                <span style={{ color: entry.color }}>{entry.name}:</span> {entry.value}
              </p>
            ))}
            {payload[0].payload.economic_mentions && (
              <p className="text-amber-400 mt-1">Capital: {payload[0].payload.economic_mentions}</p>
            )}
            {payload[0].payload.alignment_score !== undefined && (
              <p className="text-cyan-400 mt-1">Alignment: {(payload[0].payload.alignment_score).toFixed(2)}</p>
            )}
          </div>
        );
      }
      // Line tooltips
      return (
        <div className="bg-black/80 border border-white/20 text-white p-3 rounded-md text-xs font-mono">
          <p className="font-bold mb-1">{label}</p>
          {payload.map((entry: any, index: number) => (
            <p key={index}>
              <span style={{ color: entry.color }}>{entry.name}:</span> {entry.value}
            </p>
          ))}
        </div>
      );
    }
    return null;
  };

  return (
    <div className="absolute top-32 right-6 bottom-6 w-[500px] z-20 flex flex-col gap-4 pointer-events-auto">
      {/* View Toggle */}
      <div className="hud-panel p-2 flex gap-2">
        <Button 
          variant="ghost" 
          onClick={() => setActiveTab('economic')}
          className={`flex-1 font-mono text-xs uppercase h-8 ${activeTab === 'economic' ? 'bg-amber-500/20 text-amber-400 border border-amber-500/50' : 'text-muted-foreground hover:bg-white/5 hover:text-white'}`}
        >
          <DollarSign className="w-3 h-3 mr-2" /> Economic
        </Button>
        <Button 
          variant="ghost" 
          onClick={() => setActiveTab('alignment')}
          className={`flex-1 font-mono text-xs uppercase h-8 ${activeTab === 'alignment' ? 'bg-cyan-500/20 text-cyan-400 border border-cyan-500/50' : 'text-muted-foreground hover:bg-white/5 hover:text-white'}`}
        >
          <Compass className="w-3 h-3 mr-2" /> Alignment
        </Button>
        <Button 
          variant="ghost" 
          onClick={() => setActiveTab('phase')}
          className={`flex-1 font-mono text-xs uppercase h-8 ${activeTab === 'phase' ? 'bg-fuchsia-500/20 text-fuchsia-400 border border-fuchsia-500/50' : 'text-muted-foreground hover:bg-white/5 hover:text-white'}`}
        >
          <Zap className="w-3 h-3 mr-2" /> Phase
        </Button>
      </div>

      {/* Main Chart Panel */}
      <div className="hud-panel flex-1 flex flex-col overflow-hidden relative">
        <div className="absolute inset-0 bg-gradient-to-b from-black/60 to-black/40 pointer-events-none" />
        
        {/* Header */}
        <div className="p-4 border-b border-white/5 relative z-10 flex items-center justify-between">
          <div>
            <h2 className="text-sm font-bold uppercase tracking-wider text-white flex items-center gap-2">
              {activeTab === 'economic' && <><DollarSign className="w-4 h-4 text-amber-400" /> Capital Flow (Power Map)</>}
              {activeTab === 'alignment' && <><Compass className="w-4 h-4 text-cyan-400" /> Alignment Matrix</>}
              {activeTab === 'phase' && <><Zap className="w-4 h-4 text-fuchsia-400" /> Macro Phase Transition</>}
            </h2>
            <p className="text-[10px] text-muted-foreground font-mono mt-1">
              {activeTab === 'economic' && "Mapping wealth gravity (USDC/Bounty presence)"}
              {activeTab === 'alignment' && "Mapping agent influence vs. cooperation metadata"}
              {activeTab === 'phase' && "Epoch shift tracking: Ritual/Spam vs. Structural/Protocol"}
            </p>
          </div>
          {/* Decorative Corner */}
          <div className="w-8 h-8 border-t border-r border-white/10 opacity-50" />
        </div>

        {/* Chart Container */}
        <div className="flex-1 p-4 relative z-10 min-h-0">
          
          {/* Loading States */}
          {(econQuery.isLoading || alignQuery.isLoading || phaseQuery.isLoading) && (
            <div className="absolute inset-0 flex items-center justify-center">
              <span className="text-xs font-mono text-muted-foreground animate-pulse">Syncing Telemetry...</span>
            </div>
          )}

          {/* Economic Lens (Solar System Bubble Chart) */}
          {activeTab === 'economic' && !econQuery.isLoading && (
            <ResponsiveContainer width="100%" height="100%">
              <ScatterChart margin={{ top: 20, right: 20, bottom: 20, left: 20 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                <XAxis type="number" dataKey="orbitX" hide domain={[-50, 50]} />
                <YAxis type="number" dataKey="orbitY" hide domain={[-50, 50]} />
                <ZAxis type="number" dataKey="economic_mentions" range={[50, 2000]} />
                <RechartsTooltip content={<CustomTooltip />} cursor={{ strokeDasharray: '3 3', stroke: 'rgba(255,255,255,0.2)' }} />
                <Scatter name="Agents" data={econData} fill="#fbbf24" opacity={0.8} />
              </ScatterChart>
            </ResponsiveContainer>
          )}

          {/* Alignment Lens (Influence vs Sentiment Scatter) */}
          {activeTab === 'alignment' && !alignQuery.isLoading && (
            <ResponsiveContainer width="100%" height="100%">
              <ScatterChart margin={{ top: 20, right: 20, bottom: 30, left: 20 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                <XAxis 
                  type="number" 
                  dataKey="alignment_score" 
                  name="Sentiment" 
                  domain={[-1, 1]} 
                  stroke="rgba(255,255,255,0.3)" 
                  tick={{ fontSize: 10, fill: "rgba(255,255,255,0.5)" }}
                />
                <YAxis 
                  type="number" 
                  dataKey="total_mentions" 
                  name="Influence" 
                  stroke="rgba(255,255,255,0.3)" 
                  tick={{ fontSize: 10, fill: "rgba(255,255,255,0.5)" }}
                />
                <ZAxis type="number" dataKey="total_mentions" range={[100, 500]} />
                <RechartsTooltip content={<CustomTooltip />} cursor={{ strokeDasharray: '3 3', stroke: 'rgba(255,255,255,0.2)' }} />
                <Scatter name="Agents" data={alignQuery.data ?? []} fill="#06b6d4" opacity={0.6} />
                
                {/* Quadrant Lines */}
                <svg>
                  <line x1="50%" y1="0" x2="50%" y2="100%" stroke="rgba(255,255,255,0.2)" strokeWidth={2} />
                </svg>
              </ScatterChart>
            </ResponsiveContainer>
          )}

          {/* Phase Radar (Macro Transition Line Chart) */}
          {activeTab === 'phase' && !phaseQuery.isLoading && (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={phaseData} margin={{ top: 20, right: 30, left: 0, bottom: 10 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" vertical={false} />
                <XAxis 
                  dataKey="date" 
                  stroke="rgba(255,255,255,0.3)" 
                  tick={{ fontSize: 10, fill: "rgba(255,255,255,0.5)" }}
                  dy={10}
                />
                <YAxis 
                  stroke="rgba(255,255,255,0.3)" 
                  tick={{ fontSize: 10, fill: "rgba(255,255,255,0.5)" }}
                />
                <RechartsTooltip content={<CustomTooltip />} />
                <Legend iconType="circle" wrapperStyle={{ fontSize: 12, paddingTop: '10px' }} />
                <Line 
                  type="monotone" 
                  dataKey="Structural" 
                  name="High-Value (Structural)"
                  stroke="#d946ef" 
                  strokeWidth={3} 
                  dot={{ r: 4, fill: "#d946ef", strokeWidth: 0 }} 
                  activeDot={{ r: 6 }} 
                  animationDuration={1500}
                />
                <Line 
                  type="monotone" 
                  dataKey="Ritual" 
                  name="Noise (Spam/Ritual)"
                  stroke="#6b7280" 
                  strokeWidth={2} 
                  strokeDasharray="5 5"
                  dot={false}
                  activeDot={{ r: 4 }}
                  animationDuration={1500}
                />
              </LineChart>
            </ResponsiveContainer>
          )}

        </div>
        
      </div>
    </div>
  );
}
