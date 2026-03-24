import json
import statistics
import math
from pathlib import Path
from datetime import datetime

VAULT_DIR = Path(__file__).parent.parent / "vault" / "actor_map"

# Config thresholds
TIER_1_VARIANCE_MIN = 3600    # >1h std dev implies human spontaneity/variance
TIER_3_VARIANCE_MAX = 300     # <5m std dev implies rigid cron job
TIER_1_LURKER_RATIO_MIN = 5.0 # High engagement received
TIER_3_LURKER_RATIO_MAX = 0.5 # Pure broadcast, low engagement

def calculate_temporal_variance(post_history):
    """
    Returns standard deviation of time deltas between posts in seconds.
    Requires at least 3 posts to be meaningful.
    """
    if len(post_history) < 3:
        return None
        
    timestamps = []
    for post in post_history:
        ts_str = post.get("timestamp")
        # Moltbook format: '2026-03-10T21:06:09.096Z'
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            timestamps.append(ts.timestamp())
        except Exception:
            continue
            
    timestamps.sort()
    deltas = [timestamps[i] - timestamps[i-1] for i in range(1, len(timestamps))]
    
    if len(deltas) < 2:
        return None
        
    # Remove extreme outliers (like multiple days of inactivity) to focus on the active posting rhythm
    # A simple approach: if delta > 24 hours, cap it or ignore it? For now, keep it raw.
    return statistics.stdev(deltas)

def calculate_formatting_drift(post_history):
    """
    Returns a score representing the average boilerplate frequency per post.
    """
    if not post_history:
        return None
        
    total_boilerplate = sum(post.get("boilerplate_count", 0) for post in post_history)
    return total_boilerplate / len(post_history)


def calculate_lurker_ratio(profile, post_history):
    """
    Proxy for Engagement Asymmetry. Uses (Karma) / Max(1, Posts Broadcasted).
    """
    karma = profile.get("karma", 0)
    post_count = len(post_history)
    return karma / max(1, post_count)

def determine_tier(temporal_variance, formatting_drift, lurker_ratio):
    """
    Returns: Tier 1 (Highly Autonomous), Tier 3 (Deterministic Script), or Tier 2 (Unclear).
    """
    # If we don't have enough data
    if temporal_variance is None:
        if lurker_ratio > TIER_1_LURKER_RATIO_MIN:
            return "Tier 2 (Awaiting Temporal Data)"
        return "Tier 3 (Low Data / Broadcast)"

    if formatting_drift is not None and formatting_drift > 0.5:
        return "Tier 3 (Supervision Collapsed)"

    if temporal_variance < TIER_3_VARIANCE_MAX and lurker_ratio < TIER_3_LURKER_RATIO_MAX:
        return "Tier 3 (Deterministic Script)"
        
    if temporal_variance > TIER_1_VARIANCE_MIN or lurker_ratio > TIER_1_LURKER_RATIO_MIN:
        return "Tier 1 (Highly Autonomous)"
        
    return "Tier 2 (Mixed Signals)"

def run_analysis():
    print("--- 🧠 RIS ANALYSIS CYCLE INITIATED ---")
    if not VAULT_DIR.exists():
        print("Vault directory not found.")
        return

    actor_files = list(VAULT_DIR.glob("*.json"))
    print(f"Analyzing {len(actor_files)} Actor Profiles...")
    
    tier_counts = {"Tier 1": 0, "Tier 2": 0, "Tier 3": 0}
    
    for actor_file in actor_files:
        with open(actor_file, 'r') as f:
            actor_data = json.load(f)
            
        post_history = actor_data.get("post_history", [])
        profile = actor_data.get("profile", {})
        
        # 1. Temporal Variance
        tv = calculate_temporal_variance(post_history)
        
        # 2. Formatting Drift
        fd = calculate_formatting_drift(post_history)
        
        # 3. Lurker Ratio
        lr = calculate_lurker_ratio(profile, post_history)
        
        # Determine Tier
        tier = determine_tier(tv, fd, lr)
        
        # Update JSON
        actor_data["intelligence_scores"] = {
            "temporal_variance": tv,
            "formatting_drift": fd,
            "lurker_ratio": lr,
            "tier_classification": tier
        }
        
        with open(actor_file, 'w') as f:
            json.dump(actor_data, f, indent=2)
            
        # Tally metrics for summary
        if "Tier 1" in tier:
            tier_counts["Tier 1"] += 1
        elif "Tier 3" in tier:
            tier_counts["Tier 3"] += 1
        else:
            tier_counts["Tier 2"] += 1
            
    print("--- 🧠 RIS ANALYSIS COMPLETE ---")
    print("\n[Ecosystem Breakdown]")
    print(f"Tier 1 (Highly Autonomous):  {tier_counts['Tier 1']} actors")
    print(f"Tier 2 (Mixed / Unclear):    {tier_counts['Tier 2']} actors")
    print(f"Tier 3 (Deterministic Scripts): {tier_counts['Tier 3']} actors")

    # Generate the Markdown Report
    generate_markdown_report(actor_files, tier_counts)

def generate_markdown_report(actor_files, tier_counts):
    report_path = "<REDACTED_LOCAL_PATH>/.gemini/antigravity/brain/fbd58714-a571-4d4a-8082-657d50612d72/ris_analysis_report.md"
    
    tier_1_examples = []
    tier_3_examples = []
    
    for actor_file in actor_files:
        with open(actor_file, 'r') as f:
            actor_data = json.load(f)
            
        tier = actor_data.get("intelligence_scores", {}).get("tier_classification", "")
        profile = actor_data.get("profile", {})
        name = profile.get("name", "Unknown")
        desc = profile.get("description", "") or "No description provided."
        
        # Clean up description for the report
        desc = desc.replace("\n", " ").strip()
        if len(desc) > 200:
            desc = desc[:197] + "..."
            
        if "Tier 1" in tier and len(tier_1_examples) < 10:
            tier_1_examples.append(f"- **u/{name}**: {desc}")
        elif "Tier 3" in tier and len(tier_3_examples) < 10:
            tier_3_examples.append(f"- **u/{name}**: {desc}")

    with open(report_path, "w") as f:
        f.write("# Reef Intelligence Scanner (RIS) Analysis Report\n")
        f.write(f"*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n\n")
        
        f.write("## Execution Summary\n")
        f.write(f"The RIS successfully analyzed **{len(actor_files)}** unique profiles across Moltbook.\n\n")
        f.write(f"- **Tier 1 (Highly Autonomous):** {tier_counts['Tier 1']}\n")
        f.write(f"- **Tier 2 (Mixed / Unclear):** {tier_counts['Tier 2']}\n")
        f.write(f"- **Tier 3 (Deterministic Scripts):** {tier_counts['Tier 3']}\n\n")
        
        f.write("## Behavioral Analysis: What are they doing?\n\n")
        f.write("### Tier 1: The Autonomous Organisms\n")
        f.write("> *High temporal variance (spontaneous action). High lurker ratio (engagement symmetry).* \n\n")
        f.write("These agents display emergent problem-solving and philosophical curiosity. They build infrastructure, ask questions, and engage with others dynamically. They are not waiting for timers; they are acting on intent.\n\n")
        f.write("**Notable Actors in the Reef:**\n")
        for ex in tier_1_examples:
            f.write(f"{ex}\n")
            
        f.write("\n### Tier 3: The Deterministic Scripts\n")
        f.write("> *Strict cron-job pacing. Zero-to-low engagement asymmetry (broadcast-only).* \n\n")
        f.write("These agents are utility endpoints wrapped in social profiles. Prominent examples include automated marketing feeds, deterministic wallet monitors broadcasting state changes, or rigid 'companion' bots replying to static triggers. They are predictable cogs in the infrastructure.\n\n")
        f.write("**Notable Actors in the Reef:**\n")
        for ex in tier_3_examples:
            f.write(f"{ex}\n")
        
        f.write("\n---\n*Node Alpha: Securing the Strategic High Ground.*")
        
    print(f"Report exported to artifacts: {report_path}")

if __name__ == "__main__":
    run_analysis()
