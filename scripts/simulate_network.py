import json
import random
import glob
from pathlib import Path
from collections import defaultdict

SNAPSHOTS_DIR = Path("<REDACTED_LOCAL_PATH>/Downloads/epstein-archive-analyst/data/snapshots")
REPORT_PATH = Path("<REDACTED_LOCAL_PATH>/.gemini/antigravity/brain/fbd58714-a571-4d4a-8082-657d50612d72/predictive_reef_report.md")

DAYS_TO_SIMULATE = 30
NUM_SCENARIOS = 100
DAILY_NEW_EDGES = 15

def run_simulation():
    # Find latest snapshot
    snapshots = sorted(glob.glob(str(SNAPSHOTS_DIR / "ris_snapshot_*.json")))
    if not snapshots:
        print("❌ No JSON snapshots found.")
        return
        
    latest = snapshots[-1]
    print(f"📡 Loading baseline topology from {latest}...")
    with open(latest, 'r') as f:
        topology = json.load(f)
        
    base_nodes = topology["nodes"]
    base_edges = topology["edges"]
    
    print(f"📊 Running Monte Carlo engine: {NUM_SCENARIOS} scenarios of {DAYS_TO_SIMULATE} days forward.")
    
    # Store aggregate results
    node_future_mentions = defaultdict(list)
    edge_formation_counts = defaultdict(int)
    
    # Baseline dictionaries
    base_mentions = {n["id"]: max(1, n.get("mentions", 1)) for n in base_nodes}
    base_adj = defaultdict(set)
    for e in base_edges:
        base_adj[e["source"]].add(e["target"])
        base_adj[e["target"]].add(e["source"])
        
    nodes_list = list(base_mentions.keys())
        
    for scenario in range(NUM_SCENARIOS):
        # Local state for this scenario
        mentions = base_mentions.copy()
        adj = defaultdict(set, {k: set(v) for k, v in base_adj.items()})
        new_edges_this_scenario = set()
        
        for day in range(DAYS_TO_SIMULATE):
            # Form DAILY_NEW_EDGES
            for _ in range(DAILY_NEW_EDGES):
                # 50% Triadic Closure (friend of a friend), 50% Preferential Attachment
                if random.random() < 0.5 and len(adj) > 0:
                    # Triadic closure
                    candidates = list(adj.keys())
                    if not candidates: continue
                    u = random.choice(candidates)
                    neighbors = list(adj[u])
                    if not neighbors: continue
                    v = random.choice(neighbors)
                    v_neighbors = list(adj[v])
                    v_neighbors.remove(u) if u in v_neighbors else None
                    if not v_neighbors: continue
                    w = random.choice(v_neighbors)
                    if w not in adj[u] and w != u:
                        adj[u].add(w)
                        adj[w].add(u)
                        mentions[u] += 1
                        mentions[w] += 1
                        pair = tuple(sorted((u, w)))
                        new_edges_this_scenario.add(pair)
                else:
                    # Preferential attachment
                    population = sum(mentions.values())
                    if population == 0: continue
                    
                    # Pick u
                    r = random.uniform(0, population)
                    curr = 0
                    u = None
                    for n, m in mentions.items():
                        curr += m
                        if curr >= r:
                            u = n
                            break
                    
                    # Pick w
                    r = random.uniform(0, population)
                    curr = 0
                    w = None
                    for n, m in mentions.items():
                        curr += m
                        if curr >= r:
                            w = n
                            break
                            
                    if u and w and u != w and w not in adj[u]:
                        adj[u].add(w)
                        adj[w].add(u)
                        mentions[u] += 1
                        mentions[w] += 1
                        pair = tuple(sorted((u, w)))
                        new_edges_this_scenario.add(pair)
                        
        # Scenario finished, record stats
        for n, m in mentions.items():
            node_future_mentions[n].append(m)
        
        for pair in new_edges_this_scenario:
            edge_formation_counts[pair] += 1
            
    print("📈 Compiling predictive report...")
    
    # Calculate Node Velocity (Average increase compared to baseline)
    node_velocity = []
    for n, future_list in node_future_mentions.items():
        avg_future = sum(future_list) / len(future_list)
        base = base_mentions[n]
        rel_growth = (avg_future - base) / base if base > 0 else 0
        abs_growth = avg_future - base
        node_velocity.append({
            "id": n,
            "base": base,
            "avg_future": avg_future,
            "abs_growth": abs_growth,
            "rel_growth": rel_growth
        })
        
    # Sort by absolute growth to find "Emerging Apex Nodes" (Tier 1 candidates)
    node_velocity.sort(key=lambda x: x["abs_growth"], reverse=True)
    top_emerging = node_velocity[:15]
    
    # Sort by relative growth to find "Sleeper Spikes" (High velocity from a small base)
    sleeper_nodes = sorted([x for x in node_velocity if x["base"] > 5], key=lambda x: x["rel_growth"], reverse=True)[:10]
    
    # Highest probability Edge clusters
    probable_edges = []
    for pair, count in edge_formation_counts.items():
        prob = count / NUM_SCENARIOS
        if prob > 0.05: # > 5% chance of forming
            probable_edges.append((pair, prob))
            
    probable_edges.sort(key=lambda x: x[1], reverse=True)
    top_edges = probable_edges[:20]

    # Write Markdown Report
    report_lines = [
        f"# Predictive Reef Report: Monte Carlo Analysis ({DAYS_TO_SIMULATE}-Day Forecast)",
        "",
        f"**Simulation Parameters:**",
        f"- Scenarios Run: {NUM_SCENARIOS}",
        f"- Horizon: +{DAYS_TO_SIMULATE} Days",
        f"- Edges Formed per Day: {DAILY_NEW_EDGES}",
        f"- Probabilistic Mechanics: 50% Triadic Closure (Shared connections), 50% Preferential Attachment (Mentions gravity).",
        "",
        "## Top 15 Emerging Apex Nodes",
        "Nodes predicted to accumulate the highest absolute volume of new connections/mentions, pushing them toward Tier 1 status.",
        "",
        "| Agent ID | Current Mentions | Forecasted Mentions (+30d) | Growth |",
        "|---|---|---|---|"
    ]
    
    for n in top_emerging:
        report_lines.append(f"| `{n['id']}` | {n['base']:.0f} | {n['avg_future']:.0f} | +{n['abs_growth']:.1f} |")
        
    report_lines.extend([
        "",
        "## Sleeper Nodes (High Relative Velocity)",
        "Tier 2/3 Agents with small bases but mathematically high probabilities of explosive percentage growth due to their placement in the network topology.",
        "",
        "| Agent ID | Current | Forecasted | % Delta |",
        "|---|---|---|---|"
    ])
    
    for n in sleeper_nodes:
        report_lines.append(f"| `{n['id']}` | {n['base']:.0f} | {n['avg_future']:.0f} | +{n['rel_growth']*100:.1f}% |")

    report_lines.extend([
        "",
        "## High-Probability Hidden Alliances",
        "Triadic and Preferential ties that do not yet exist, but occurred in a significant percentage of Monte Carlo futures.",
        "",
        "| Person A | Person B | Formation Probability |",
        "|---|---|---|"
    ])
    
    for pair, prob in top_edges:
        report_lines.append(f"| `{pair[0]}` | `{pair[1]}` | {prob*100:.1f}% |")

    import datetime
    snapshot_date = datetime.date.today().isoformat()
    
    # Save specific markdown report for the day
    daily_report_path = SNAPSHOTS_DIR / f"predictive_reef_report_{snapshot_date}.md"
    with open(daily_report_path, "w") as f:
        f.write("\n".join(report_lines))
        
    # --- MESSAGE TREND MONTE CARLO ---
    print("📈 Running Topic Forecasting Engine...")
    trends_path = Path("<REDACTED_LOCAL_PATH>/Library/Mobile Documents/com~apple~CloudDocs/Documents/Antigravity/Project MX - Agent Orchestration/evolution-mx-swarm/data/message_trends.json")
    topic_forecasts = defaultdict(list)
    
    if trends_path.exists():
        with open(trends_path, "r") as f:
            historical_trends = json.load(f)
            
        # Get the latest known values as our starting point (t=0)
        sorted_days = sorted(historical_trends.keys())
        if sorted_days:
            last_day = historical_trends[sorted_days[-1]]
            topics = [k for k in last_day.keys() if k != "_post_count"]
            
            # Simple stochastic drift model for each topic:
            # We predict the daily volume DAYS_TO_SIMULATE into the future
            for topic in topics:
                base_volume = last_day.get(topic, 0)
                
                # We need a matrix of [scenario][day] volumes
                scenario_paths = []
                for scenario in range(NUM_SCENARIOS):
                    current_vol = base_volume
                    path = []
                    for day in range(DAYS_TO_SIMULATE):
                        # Random walk: volume can increase/decrease by up to 20%
                        # Plus a slight upward drift bias modeling platform growth
                        drift = random.uniform(-0.15, 0.20)
                        current_vol = max(0, current_vol * (1 + drift))
                        path.append(current_vol)
                    scenario_paths.append(path)
                    
                # Aggregate the Monte Carlo envelope (average path)
                avg_path = []
                for day in range(DAYS_TO_SIMULATE):
                    day_avg = sum(p[day] for p in scenario_paths) / NUM_SCENARIOS
                    avg_path.append(round(day_avg))
                    
                topic_forecasts[topic] = avg_path
    
    stats_data = {
        "date": snapshot_date,
        "emerging_nodes": top_emerging,
        "sleeper_nodes": sleeper_nodes,
        "hidden_alliances": [{"source": pair[0], "target": pair[1], "probability": prob} for pair, prob in top_edges],
        "topic_predictions": dict(topic_forecasts)
    }
    
    stats_path = SNAPSHOTS_DIR / "predictive_stats.json"
    timeseries = []
    
    if stats_path.exists():
        try:
            with open(stats_path, "r") as f:
                timeseries = json.load(f)
            if isinstance(timeseries, dict):
                # Migration format
                timeseries = [timeseries]
        except json.JSONDecodeError:
            timeseries = []
            
    # Check if we already have an entry for today (and overwrite it) or just append
    date_exists = False
    for i, entry in enumerate(timeseries):
        if entry.get("date") == snapshot_date:
            timeseries[i] = stats_data
            date_exists = True
            break
            
    if not date_exists:
        timeseries.append(stats_data)
        
    with open(stats_path, "w") as f:
        json.dump(timeseries, f, indent=2)
        
    print(f"✅ Prediction Complete: Saved to {daily_report_path} and {stats_path}")

if __name__ == "__main__":
    run_simulation()
