import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';

export type AgentRole = 'mayor' | 'polecat';
export type AgentStatus = 'active' | 'idle' | 'waiting' | 'completed';

export interface Bead {
  id: string;
  title: string;
  status: 'assigned' | 'in_progress' | 'completed' | 'blocked';
  priority: 'low' | 'medium' | 'high' | 'critical';
}

export interface MailboxMessage {
  id: string;
  from: string;
  type: 'task_assignment' | 'handoff' | 'status_check' | 'priority_change' | 'dependency' | 'convoy_update';
  content: string;
  timestamp: string;
  read: boolean;
}

export interface ActivityLogEntry {
  id: string;
  action: string;
  description: string;
  timestamp: string;
  details?: string;
}

export interface HookState {
  lastCommit: string;
  worktree: string;
  filesModified: number;
  lastSync: string;
}

export interface CodeChange {
  id: string;
  filePath: string;
  lineStart: number;
  lineEnd: number;
  oldCode: string;
  newCode: string;
  status: 'pending' | 'approved' | 'rejected';
  timestamp: string;
}

export interface NodeData {
  id: string;
  name: string;
  role: string;
  company: string;
  img: string;
  exceptional: boolean;
  skills: string[];
  psychographic: {
    openness: number;
    conscientiousness: number;
    extraversion: number;
    agreeableness: number;
    neuroticism: number;
    innovationScore: number;
    leadershipPotential: number;
  };
  social: {
    github: string;
    linkedin: string;
    twitter: string;
    website: string;
  };
  yearsExperience: number;
  location: string;
  clusterGroup: number;
  journey: {
    milestones: { version: string; event: string; category: string }[];
    narrative: string;
    exceptionalTraits: string[];
  };
  agentRole: AgentRole;
  agentStatus: AgentStatus;
  assignedRig: { id: string; name: string; repo: string } | null;
  currentConvoy: { id: string; name: string; priority: string } | null;
  beads: Bead[];
  mailbox: MailboxMessage[];
  activityLog: ActivityLogEntry[];
  hookState: HookState | null;
  upstreamAgentId: string | null;
  downstreamAgentId: string | null;
  isEphemeral: boolean;
  spawnTime: string;
  taskCompletionRate: number;
  currentFile: string | null;
  codeChanges: CodeChange[];
}

export interface GraphData {
  nodes: NodeData[];
  links: { source: string; target: string }[];
}

export interface MetricThreshold {
  op: string;
  target: number;
  pass: boolean;
}

export interface FormulaCard {
  metric: string;
  value: number | string | boolean | null;
  formula: string;
  unit: string;
  description: string;
  threshold: MetricThreshold | null;
}

export interface EventSourcedMetrics {
  source: string;
  metrics: Record<string, number | string | boolean>;
  metric_dictionary: Record<string, { formula: string; unit: string; description: string }>;
  formula_cards: FormulaCard[];
  gate_a_ready: boolean;
}

export interface TraceSpan {
  span_id: string;
  trace_id: string;
  parent_span_id: string | null;
  service: string;
  operation: string;
  task_id: string | null;
  attempt_id: string | null;
  status: string;
  actor_id: string;
  timestamp: string;
  attributes: Record<string, unknown>;
}

export interface TraceEvent {
  event_id: string;
  trace_id: string;
  task_id: string;
  attempt_id: string;
  state_from: string;
  state_to: string;
  reason: string | null;
  provider: string;
  timestamp: string;
  actor_id: string;
}

export interface TraceListResponse {
  trace_spans: TraceSpan[];
  count: number;
}

export interface TraceBundle {
  trace_id: string;
  span_count: number;
  event_count: number;
  services: string[];
  spans: TraceSpan[];
  events: TraceEvent[];
}

export interface SubmissionEvent {
  event_id: string;
  task_id: string;
  trace_id: string;
  attempt_id: string;
  branch_name: string;
  submission_id: string;
  submission_type: string;
  provider: string;
  url?: string | null;
  status: string;
  goal: string;
  affected_paths: string[];
  local_mode: boolean;
  created_at: string;
  settled_at?: string | null;
  reason?: string | null;
}

export interface RecentSubmissionsResponse {
  submissions: SubmissionEvent[];
  count: number;
}

interface SwarmStatus {
  agents: {
    total: number;
    active_60s: number;
    idle: number;
  };
  governor?: {
    state?: string;
  };
  teamplan?: {
    streams?: Array<{ name: string; role: string }>;
  } | null;
  active_tasks?: Array<{
    id: string;
    worker?: string;
    status?: string;
    progress?: number;
    goal?: string;
    risk_tier?: number;
    created_at?: string;
  }>;
  submissions?: {
    latest_successful_branch?: string | null;
    latest_successful_submission_id?: string | null;
    latest_successful_submission_url?: string | null;
    latest_successful_trace_id?: string | null;
    latest_settlement_timestamp?: string | null;
    recent_settlement_count?: number;
  };
}

// ============================================================================
// V2 Gas Town Analytical Lenses Interfaces
// ============================================================================

export interface EconomicNode {
  id: string;
  name: string;
  economic_mentions: number;
}

export interface AlignmentNode {
  id: string;
  name: string;
  cooperative: number;
  adversarial: number;
  alignment_score: number;
  total_mentions: number;
}

export type PhaseTransitionData = Record<string, {
  Structural: number;
  Ritual: number;
  _post_count: number;
}>;

export interface Rig {
  id: string;
  name: string;
  repo: string;
}

const defaultRigs: Rig[] = [
  { id: 'rig-frontend', name: 'Frontend App', repo: 'frontend' },
  { id: 'rig-backend', name: 'Backend API', repo: 'backend' },
  { id: 'rig-infra', name: 'Infrastructure', repo: 'infra' },
];

export const convoyColors: Record<string, string> = {
  'convoy-auth': '#f59e0b',
  'convoy-perf': '#10b981',
  'convoy-ui': '#8b5cf6',
  'convoy-api': '#ef4444',
  'convoy-test': '#06b6d4',
  'convoy-docs': '#ec4899',
};

export const statusColors: Record<AgentStatus, string> = {
  active: '#22c55e',
  idle: '#6b7280',
  waiting: '#f59e0b',
  completed: '#3b82f6',
};

const avatarSeeds = [
  'polecat1',
  'polecat2',
  'polecat3',
  'polecat4',
  'polecat5',
  'polecat6',
  'polecat7',
  'polecat8',
];

const defaultPsychographic = {
  openness: 0.5,
  conscientiousness: 0.5,
  extraversion: 0.5,
  agreeableness: 0.5,
  neuroticism: 0.5,
  innovationScore: 0.5,
  leadershipPotential: 0.5,
};

const emptyJourney = {
  milestones: [],
  narrative: 'No narrative available.',
  exceptionalTraits: [],
};

function getAvatar(seed: string) {
  const index = Math.abs(seed.length) % avatarSeeds.length;
  const avatarSeed = avatarSeeds[index];
  return `https://api.dicebear.com/7.x/bottts/svg?seed=${avatarSeed}&backgroundColor=1a1c23`;
}

function toTitleCase(value: string) {
  return value
    .split(/[\s_-]+/)
    .filter(Boolean)
    .map(part => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

function normalizeWorkerName(workerId: string, index: number) {
  if (!workerId) {
    return `Worker ${String(index + 1).padStart(2, '0')}`;
  }
  if (workerId.includes('worker')) {
    return toTitleCase(workerId.replace(/[-_]/g, ' '));
  }
  return toTitleCase(workerId);
}

function mapGovernorStateToStatus(state?: string): AgentStatus {
  if (!state) return 'active';
  const normalized = state.toLowerCase();
  if (normalized.includes('freeze') || normalized.includes('paused')) {
    return 'idle';
  }
  if (normalized.includes('throttle')) {
    return 'waiting';
  }
  return 'active';
}

function mapTaskStatus(status?: string): Bead['status'] {
  if (!status) return 'assigned';
  const normalized = status.toLowerCase();
  if (normalized.includes('merge') || normalized.includes('complete')) return 'completed';
  if (normalized.includes('fail')) return 'blocked';
  if (normalized.includes('run')) return 'in_progress';
  return 'assigned';
}

function mapTaskPriority(riskTier?: number): Bead['priority'] {
  if (riskTier === 0) return 'low';
  if (riskTier === 1) return 'medium';
  if (riskTier === 2) return 'high';
  return 'medium';
}

function makeNodeBase(id: string, name: string): Omit<NodeData, 'agentRole' | 'agentStatus' | 'assignedRig' | 'currentConvoy' | 'beads'> {
  return {
    id,
    name,
    role: 'Agent',
    company: 'MX Swarm',
    img: getAvatar(id),
    exceptional: false,
    skills: [],
    psychographic: { ...defaultPsychographic },
    social: {
      github: '',
      linkedin: '',
      twitter: '',
      website: '',
    },
    yearsExperience: 0,
    location: 'Unknown',
    clusterGroup: 0,
    journey: { ...emptyJourney },
    mailbox: [],
    activityLog: [],
    hookState: null,
    upstreamAgentId: null,
    downstreamAgentId: null,
    isEphemeral: false,
    spawnTime: 'unknown',
    taskCompletionRate: 0,
    currentFile: null,
    codeChanges: [],
  };
}

function buildRigs(status: SwarmStatus | undefined): Rig[] {
  const streams = status?.teamplan?.streams;
  if (!streams || streams.length === 0) {
    return defaultRigs;
  }

  return streams.map((stream, index) => ({
    id: `rig-${index}-${stream.name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`,
    name: stream.name,
    repo: stream.role,
  }));
}

export function mapSwarmStatusToGraph(status?: SwarmStatus): { graph: GraphData; rigs: Rig[] } {
  const rigs = buildRigs(status);
  if (!status) {
    return { graph: { nodes: [], links: [] }, rigs };
  }
  const tasks = status?.active_tasks ?? [];
  const workerIds = new Set<string>();
  const workerTasks = new Map<string, typeof tasks>();

  tasks.forEach(task => {
    if (!task.worker) return;
    workerIds.add(task.worker);
    const existing = workerTasks.get(task.worker) ?? [];
    existing.push(task);
    workerTasks.set(task.worker, existing);
  });

  const totalWorkers = Math.max(status?.agents?.total ?? 0, workerIds.size);
  const idleCount = Math.max(0, status?.agents?.idle ?? 0);
  const governorNodeId = 'governor';

  const nodes: NodeData[] = [];
  const links: { source: string; target: string }[] = [];

  const governor = {
    ...makeNodeBase(governorNodeId, 'Governor'),
    role: 'Orchestrator',
    agentRole: 'mayor' as AgentRole,
    agentStatus: mapGovernorStateToStatus(status?.governor?.state),
    assignedRig: null,
    currentConvoy: null,
    beads: [],
    exceptional: true,
    isEphemeral: false,
  };
  nodes.push(governor);

  const workerIdList = Array.from(workerIds);
  for (let i = workerIdList.length; i < totalWorkers; i += 1) {
    workerIdList.push(`worker-${i + 1}`);
  }

  workerIdList.forEach((workerId, index) => {
    const statusValue: AgentStatus = index < idleCount ? 'idle' : 'active';
    const tasksForWorker = workerTasks.get(workerId) ?? [];
    const beads: Bead[] = tasksForWorker.map(task => ({
      id: task.id,
      title: task.goal || 'Assigned task',
      status: mapTaskStatus(task.status),
      priority: mapTaskPriority(task.risk_tier),
    }));

    const completed = beads.filter(bead => bead.status === 'completed').length;
    const completionRate = beads.length > 0 ? Math.round((completed / beads.length) * 100) : 0;

    const rig = rigs.length > 0 ? rigs[index % rigs.length] : null;
    const convoyKeys = Object.keys(convoyColors);
    const convoyId = convoyKeys.length > 0 ? convoyKeys[index % convoyKeys.length] : null;

    const node: NodeData = {
      ...makeNodeBase(workerId, normalizeWorkerName(workerId, index)),
      role: rig?.name || 'Agent',
      agentRole: 'polecat',
      agentStatus: statusValue,
      assignedRig: rig,
      currentConvoy: convoyId ? { id: convoyId, name: toTitleCase(convoyId.replace('convoy-', '')), priority: 'medium' } : null,
      beads,
      exceptional: beads.length > 0,
      isEphemeral: workerId.startsWith('sim-') || workerId.startsWith('swarm-') || workerId.startsWith('worker-'),
      spawnTime: 'recent',
      taskCompletionRate: completionRate,
      activityLog: beads.slice(0, 2).map((bead, beadIndex) => ({
        id: `${bead.id}-${beadIndex}`,
        action: bead.status === 'completed' ? 'bead_completed' : 'bead_started',
        description: bead.title,
        timestamp: bead.status === 'completed' ? '5m ago' : '2m ago',
      })),
    };

    nodes.push(node);
    links.push({ source: governorNodeId, target: workerId });
  });

  return { graph: { nodes, links }, rigs };
}

function getApiBase() {
  const rawBase = (import.meta as { env?: Record<string, string> }).env?.VITE_API_BASE ?? '';
  if (!rawBase) return '';
  return rawBase.endsWith('/') ? rawBase.slice(0, -1) : rawBase;
}

async function fetchSwarmStatus(): Promise<SwarmStatus> {
  const apiBase = getApiBase();
  const res = await fetch(`${apiBase}/api/status`, { credentials: 'include' });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  return (await res.json()) as SwarmStatus;
}

async function fetchEventSourcedMetrics(windowMinutes = 60): Promise<EventSourcedMetrics> {
  const apiBase = getApiBase();
  const res = await fetch(
    `${apiBase}/api/metrics/event_sourced?window_minutes=${windowMinutes}`,
    { credentials: 'include' },
  );
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  return (await res.json()) as EventSourcedMetrics;
}

async function fetchTraceSpans(limit = 200): Promise<TraceListResponse> {
  const apiBase = getApiBase();
  const res = await fetch(`${apiBase}/api/traces?limit=${limit}`, { credentials: 'include' });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  return (await res.json()) as TraceListResponse;
}

async function fetchTraceBundle(traceId: string, limit = 500): Promise<TraceBundle> {
  const apiBase = getApiBase();
  const res = await fetch(
    `${apiBase}/api/traces/${encodeURIComponent(traceId)}?limit=${limit}`,
    { credentials: 'include' },
  );
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  return (await res.json()) as TraceBundle;
}

async function fetchRecentSubmissions(limit = 12): Promise<RecentSubmissionsResponse> {
  const apiBase = getApiBase();
  const res = await fetch(
    `${apiBase}/api/submissions/recent?limit=${limit}&settled_only=true`,
    { credentials: 'include' },
  );
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  return (await res.json()) as RecentSubmissionsResponse;
}

// ============================================================================
// V2 Gas Town Analytical Lenses Fetchers
// ============================================================================

async function fetchEconomicFlow(): Promise<EconomicNode[]> {
  const apiBase = getApiBase();
  const res = await fetch(`${apiBase}/api/v2/economic-flow`, { credentials: 'include' });
  if (!res.ok) throw new Error(await res.text() || res.statusText);
  return (await res.json()) as EconomicNode[];
}

async function fetchAlignmentMatrix(): Promise<AlignmentNode[]> {
  const apiBase = getApiBase();
  const res = await fetch(`${apiBase}/api/v2/alignment-matrix`, { credentials: 'include' });
  if (!res.ok) throw new Error(await res.text() || res.statusText);
  return (await res.json()) as AlignmentNode[];
}

async function fetchPhaseTransition(): Promise<PhaseTransitionData> {
  const apiBase = getApiBase();
  const res = await fetch(`${apiBase}/api/v2/phase-transition`, { credentials: 'include' });
  if (!res.ok) throw new Error(await res.text() || res.statusText);
  return (await res.json()) as PhaseTransitionData;
}

export function useSwarmQuery() {
  const query = useQuery({
    queryKey: ['swarm-status'],
    queryFn: fetchSwarmStatus,
    refetchInterval: 3000,
  });

  const mapped = useMemo(() => mapSwarmStatusToGraph(query.data), [query.data]);

  return {
    ...query,
    graphData: mapped.graph,
    rigs: mapped.rigs,
  };
}

export function useEventObservabilityQuery(windowMinutes = 60) {
  return useQuery({
    queryKey: ['event-observability', windowMinutes],
    queryFn: () => fetchEventSourcedMetrics(windowMinutes),
    refetchInterval: 3000,
  });
}

export function useTraceSpansQuery(limit = 200) {
  return useQuery({
    queryKey: ['trace-spans', limit],
    queryFn: () => fetchTraceSpans(limit),
    refetchInterval: 5000,
  });
}

export function useTraceBundleQuery(traceId: string | null, limit = 500) {
  return useQuery({
    queryKey: ['trace-bundle', traceId, limit],
    queryFn: () => fetchTraceBundle(traceId!, limit),
    enabled: Boolean(traceId),
    refetchInterval: 5000,
  });
}

export function useRecentSubmissionsQuery(limit = 12) {
  return useQuery({
    queryKey: ['recent-submissions', limit],
    queryFn: () => fetchRecentSubmissions(limit),
    refetchInterval: 5000,
  });
}

// ============================================================================
// V2 Gas Town Analytical Lenses Hooks
// ============================================================================

export function useEconomicFlowQuery() {
  return useQuery({
    queryKey: ['economic-flow'],
    queryFn: fetchEconomicFlow,
    refetchInterval: 10000,
  });
}

export function useAlignmentMatrixQuery() {
  return useQuery({
    queryKey: ['alignment-matrix'],
    queryFn: fetchAlignmentMatrix,
    refetchInterval: 10000,
  });
}

export function usePhaseTransitionQuery() {
  return useQuery({
    queryKey: ['phase-transition'],
    queryFn: fetchPhaseTransition,
    refetchInterval: 10000,
  });
}
