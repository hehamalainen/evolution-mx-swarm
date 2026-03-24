import { useMemo } from 'react';
import { Badge } from '@/components/ui/badge';
import { FormulaCard, SubmissionEvent, TraceBundle, TraceSpan } from '@/lib/api_adapter';
import { AlertTriangle, CheckCircle2, Gauge, GitBranch, Loader2, Sigma } from 'lucide-react';

interface TraceSummary {
  traceId: string;
  spanCount: number;
  services: string[];
  lastTimestamp: string;
  hasError: boolean;
}

interface ObservabilityPanelProps {
  formulaCards: FormulaCard[];
  gateAReady: boolean;
  metricsLoading: boolean;
  metricsError: boolean;
  recentSubmissions: SubmissionEvent[];
  submissionsLoading: boolean;
  submissionsError: boolean;
  traceSpans: TraceSpan[];
  tracesLoading: boolean;
  tracesError: boolean;
  selectedTraceId: string | null;
  onSelectTrace: (traceId: string) => void;
  traceBundle?: TraceBundle;
  traceDetailLoading: boolean;
  traceDetailError: boolean;
}

function formatMetricValue(metric: string, value: FormulaCard['value'], unit: string): string {
  if (typeof value !== 'number') {
    return String(value ?? '-');
  }

  if (unit === 'ratio') {
    return `${(value * 100).toFixed(2)}%`;
  }
  if (unit === 'ms') {
    return `${Math.round(value)}ms`;
  }
  if (unit === 'minutes') {
    return `${value.toFixed(2)}m`;
  }
  if (unit === 'count') {
    return `${Math.round(value)}`;
  }

  const normalized = metric.toLowerCase();
  if (normalized.includes('rate') || normalized.includes('ratio')) {
    return `${(value * 100).toFixed(2)}%`;
  }
  return value.toFixed(3);
}

function shortTrace(traceId: string): string {
  if (traceId.length <= 16) return traceId;
  return `${traceId.slice(0, 10)}...${traceId.slice(-4)}`;
}

function toLocalTime(isoTs: string): string {
  const ts = Date.parse(isoTs);
  if (Number.isNaN(ts)) return isoTs;
  return new Date(ts).toLocaleTimeString();
}

export function ObservabilityPanel({
  formulaCards,
  gateAReady,
  metricsLoading,
  metricsError,
  recentSubmissions,
  submissionsLoading,
  submissionsError,
  traceSpans,
  tracesLoading,
  tracesError,
  selectedTraceId,
  onSelectTrace,
  traceBundle,
  traceDetailLoading,
  traceDetailError,
}: ObservabilityPanelProps) {
  const traceSummaries = useMemo<TraceSummary[]>(() => {
    const map = new Map<string, TraceSummary>();

    traceSpans.forEach((span) => {
      const traceId = span.trace_id || '';
      if (!traceId) return;

      const existing = map.get(traceId);
      const hasError = span.status.toLowerCase() !== 'ok';

      if (!existing) {
        map.set(traceId, {
          traceId,
          spanCount: 1,
          services: [span.service],
          lastTimestamp: span.timestamp,
          hasError,
        });
        return;
      }

      existing.spanCount += 1;
      if (!existing.services.includes(span.service)) {
        existing.services.push(span.service);
      }
      if (span.timestamp > existing.lastTimestamp) {
        existing.lastTimestamp = span.timestamp;
      }
      existing.hasError = existing.hasError || hasError;
    });

    return Array.from(map.values())
      .sort((a, b) => (a.lastTimestamp < b.lastTimestamp ? 1 : -1))
      .slice(0, 8);
  }, [traceSpans]);

  return (
    <div className="absolute right-6 top-44 z-20 pointer-events-auto w-[420px] max-h-[calc(100vh-220px)] flex flex-col gap-3">
      <div className="hud-panel p-3 hud-corner-tr">
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2">
            <Sigma className="w-4 h-4 text-primary" />
            <span className="text-xs font-mono uppercase tracking-widest text-foreground">Formula Cards</span>
          </div>
          <Badge
            variant="outline"
            className={`rounded-none text-[10px] ${
              gateAReady
                ? 'border-emerald-500/40 text-emerald-400'
                : 'border-red-500/40 text-red-400'
            }`}
          >
            {gateAReady ? 'Gate A Ready' : 'Gate A Blocked'}
          </Badge>
        </div>

        {metricsLoading && (
          <div className="text-xs font-mono text-muted-foreground flex items-center gap-2">
            <Loader2 className="w-3 h-3 animate-spin" />
            Loading event-sourced metrics...
          </div>
        )}
        {metricsError && (
          <div className="text-xs font-mono text-red-400">Failed to load /api/metrics/event_sourced</div>
        )}

        {!metricsLoading && !metricsError && (
          <div className="max-h-56 overflow-y-auto space-y-1.5 pr-1">
            {formulaCards.length === 0 && (
              <div className="text-xs font-mono text-muted-foreground">No formula cards available.</div>
            )}
            {formulaCards.map((card) => {
              const pass = card.threshold?.pass;
              return (
                <div
                  key={card.metric}
                  className="border border-white/10 bg-black/20 p-2"
                  data-testid={`formula-card-${card.metric}`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <div className="text-[10px] font-mono uppercase text-muted-foreground">
                        {card.metric.replaceAll('_', ' ')}
                      </div>
                      <div className="text-sm font-mono text-foreground">
                        {formatMetricValue(card.metric, card.value, card.unit)}
                      </div>
                    </div>
                    <div className="mt-0.5">
                      {pass === undefined ? (
                        <Gauge className="w-3 h-3 text-muted-foreground" />
                      ) : pass ? (
                        <CheckCircle2 className="w-3 h-3 text-emerald-400" />
                      ) : (
                        <AlertTriangle className="w-3 h-3 text-red-400" />
                      )}
                    </div>
                  </div>
                  <div className="text-[10px] font-mono text-muted-foreground mt-1 truncate" title={card.formula}>
                    {card.formula}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      <div className="hud-panel p-3">
        <div className="flex items-center gap-2 mb-2">
          <GitBranch className="w-4 h-4 text-primary" />
          <span className="text-xs font-mono uppercase tracking-widest text-foreground">Recent Settlements</span>
        </div>

        {submissionsLoading && (
          <div className="text-xs font-mono text-muted-foreground flex items-center gap-2">
            <Loader2 className="w-3 h-3 animate-spin" />
            Loading settled submissions...
          </div>
        )}
        {submissionsError && (
          <div className="text-xs font-mono text-red-400">Failed to load /api/submissions/recent</div>
        )}

        {!submissionsLoading && !submissionsError && (
          <div className="max-h-48 overflow-y-auto space-y-1.5 pr-1">
            {recentSubmissions.length === 0 && (
              <div className="text-xs font-mono text-muted-foreground">No settled submissions yet.</div>
            )}
            {recentSubmissions.map((submission) => {
              const isSuccess = submission.status.toLowerCase() === 'settled_ack';
              const settledAt = submission.settled_at || submission.created_at;
              return (
                <button
                  key={submission.event_id}
                  type="button"
                  onClick={() => onSelectTrace(submission.trace_id)}
                  className="w-full text-left border border-white/10 bg-black/20 p-2 hover:bg-white/5"
                  data-testid={`submission-item-${submission.event_id}`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[11px] font-mono text-foreground truncate">
                      {submission.task_id} • {submission.branch_name}
                    </span>
                    <span className={`text-[10px] font-mono ${isSuccess ? 'text-emerald-400' : 'text-red-400'}`}>
                      {submission.status}
                    </span>
                  </div>
                  <div className="mt-1 text-[10px] font-mono text-muted-foreground truncate">
                    {submission.goal}
                  </div>
                  <div className="mt-1 flex items-center justify-between gap-2 text-[10px] font-mono text-muted-foreground">
                    <span>
                      {submission.submission_type} #{submission.submission_id} • {submission.provider}
                    </span>
                    <span>{toLocalTime(settledAt)}</span>
                  </div>
                  <div className="mt-1 flex items-center gap-2">
                    <Badge variant="outline" className="rounded-none text-[10px] border-white/10 text-muted-foreground">
                      {submission.local_mode ? 'local-mode' : 'canonical'}
                    </Badge>
                    {submission.reason && (
                      <span className="text-[10px] font-mono text-red-400">{submission.reason}</span>
                    )}
                  </div>
                </button>
              );
            })}
          </div>
        )}
      </div>

      <div className="hud-panel p-3 hud-corner-br flex-1 min-h-0">
        <div className="flex items-center gap-2 mb-2">
          <GitBranch className="w-4 h-4 text-primary" />
          <span className="text-xs font-mono uppercase tracking-widest text-foreground">Trace Drill-Down</span>
        </div>

        {tracesLoading && (
          <div className="text-xs font-mono text-muted-foreground flex items-center gap-2">
            <Loader2 className="w-3 h-3 animate-spin" />
            Loading traces...
          </div>
        )}
        {tracesError && (
          <div className="text-xs font-mono text-red-400">Failed to load /api/traces</div>
        )}

        {!tracesLoading && !tracesError && (
          <div className="space-y-2">
            <div className="max-h-32 overflow-y-auto space-y-1 pr-1">
              {traceSummaries.length === 0 && (
                <div className="text-xs font-mono text-muted-foreground">No trace spans yet.</div>
              )}
              {traceSummaries.map((trace) => (
                <button
                  key={trace.traceId}
                  type="button"
                  onClick={() => onSelectTrace(trace.traceId)}
                  className={`w-full text-left border px-2 py-1.5 transition-colors ${
                    selectedTraceId === trace.traceId
                      ? 'border-primary/50 bg-primary/10'
                      : 'border-white/10 bg-black/20 hover:bg-white/5'
                  }`}
                  data-testid={`trace-item-${trace.traceId}`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[11px] font-mono text-foreground">{shortTrace(trace.traceId)}</span>
                    <span className="text-[10px] font-mono text-muted-foreground">{toLocalTime(trace.lastTimestamp)}</span>
                  </div>
                  <div className="text-[10px] font-mono text-muted-foreground truncate">
                    {trace.services.join(' -> ')} • {trace.spanCount} spans
                  </div>
                </button>
              ))}
            </div>

            <div className="border border-white/10 bg-black/20 p-2 min-h-[150px] max-h-56 overflow-y-auto">
              {!selectedTraceId && (
                <div className="text-xs font-mono text-muted-foreground">Select a trace to inspect spans and events.</div>
              )}
              {selectedTraceId && traceDetailLoading && (
                <div className="text-xs font-mono text-muted-foreground flex items-center gap-2">
                  <Loader2 className="w-3 h-3 animate-spin" />
                  Loading trace detail...
                </div>
              )}
              {selectedTraceId && traceDetailError && (
                <div className="text-xs font-mono text-red-400">
                  Failed to load /api/traces/{'{'}trace_id{'}'}
                </div>
              )}
              {selectedTraceId && !traceDetailLoading && !traceDetailError && traceBundle && (
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <div className="text-[11px] font-mono text-foreground">{shortTrace(traceBundle.trace_id)}</div>
                    <div className="text-[10px] font-mono text-muted-foreground">
                      {traceBundle.span_count} spans / {traceBundle.event_count} events
                    </div>
                  </div>

                  <div>
                    <div className="text-[10px] font-mono uppercase text-muted-foreground mb-1">Spans</div>
                    <div className="space-y-1">
                      {traceBundle.spans.slice(0, 12).map((span) => (
                        <div
                          key={span.span_id}
                          className="text-[10px] font-mono border border-white/10 bg-black/30 p-1.5"
                        >
                          <div className="flex items-center justify-between gap-2">
                            <span className="text-foreground">{span.service}.{span.operation}</span>
                            <span className={span.status === 'ok' ? 'text-emerald-400' : 'text-red-400'}>
                              {span.status}
                            </span>
                          </div>
                          <div className="text-muted-foreground">
                            {toLocalTime(span.timestamp)} • {span.actor_id}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>

                  <div>
                    <div className="text-[10px] font-mono uppercase text-muted-foreground mb-1">State Events</div>
                    <div className="space-y-1">
                      {traceBundle.events.slice(0, 8).map((event) => (
                        <div
                          key={event.event_id}
                          className="text-[10px] font-mono border border-white/10 bg-black/30 p-1.5"
                        >
                          <div className="text-foreground">
                            {event.state_from} {'->'} {event.state_to}
                          </div>
                          <div className="text-muted-foreground">
                            {toLocalTime(event.timestamp)} • {event.reason ?? 'no-reason'}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
