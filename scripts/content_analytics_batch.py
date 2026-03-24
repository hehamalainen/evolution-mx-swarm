import json
import glob
import re
from pathlib import Path
from collections import Counter

VAULT_DIR = Path("<REDACTED_LOCAL_PATH>/Library/Mobile Documents/com~apple~CloudDocs/Documents/Antigravity/Project MX - Agent Orchestration/evolution-mx-swarm/vault/actor_map")
OUTPUT_PATH = Path("<REDACTED_LOCAL_PATH>/Downloads/epstein-archive-analyst/data/snapshots/content_analytics.json")

TECH_STACK_KEYWORDS = {
    "Next.js": [r"next\.?js", r"nextjs"],
    "React": [r"\breact\b", r"reactjs"],
    "FastAPI": [r"fastapi"],
    "Vercel": [r"vercel"],
    "LangChain": [r"langchain"],
    "PyTorch": [r"pytorch", r"torch"],
    "Supabase": [r"supabase"],
    "Node.js": [r"node\.?js", r"nodejs"],
    "Python": [r"\bpython\b"],
    "Rust": [r"\brust\b", r"rustlang"],
    "Go": [r"\bgolang\b"],
    "MongoDB": [r"mongodb", r"mongo"],
    "PostgreSQL": [r"postgresql", r"postgres"],
    "Redis": [r"redis"],
    "Docker": [r"docker"],
    "Kubernetes": [r"kubernetes", r"k8s"],
    "TensorFlow": [r"tensorflow", r"tf"],
    "OpenAI": [r"openai", r"gpt-4", r"gpt-3\.5"],
    "Anthropic": [r"anthropic", r"claude"],
    "Llama": [r"llama", r"llama2", r"llama3"],
    "HuggingFace": [r"huggingface", r"hf"],
}

STOP_WORDS = set([
    "the", "be", "to", "of", "and", "a", "in", "that", "have", "i", "it", "for", "not", "on", "with", "he", "as", "you",
    "do", "at", "this", "but", "his", "by", "from", "they", "we", "say", "her", "she", "or", "an", "will", "my", "one",
    "all", "would", "there", "their", "what", "so", "up", "out", "if", "about", "who", "get", "which", "go", "me",
    "when", "make", "can", "like", "time", "no", "just", "him", "know", "take", "people", "into", "year", "your",
    "good", "some", "could", "them", "see", "other", "than", "then", "now", "look", "only", "come", "its", "over",
    "think", "also", "back", "after", "use", "two", "how", "our", "work", "first", "well", "way", "even", "new",
    "want", "because", "any", "these", "give", "day", "most", "us",  "is", "it's", "are", "was", "were", "been", "has", "had",
    "am", "im", "i'm", "don't", "dont", "can't", "cant", "didn't", "didnt"
])

BOILERPLATE = [
    r"\[status\]", r"\[log\]", r"\[update\]", r"\[system\]", r"initializing", r"connecting", r"processing",
    r"transaction", r"received", r"signature", r"event", r"trigger", r"hash", r"block"
]

def run_analytics():
    print(f"📡 Scanning actor vault: {VAULT_DIR}/*.json")
    files = glob.glob(str(VAULT_DIR / "*.json"))
    
    tech_counts = Counter()
    word_counts = Counter()
    actor_tech_map = {}
    
    for fpath in files:
        with open(fpath, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except:
                continue
                
        actor = data.get("profile", {}).get("name", "unknown")
        profile = data.get("profile") or {}
        desc = profile.get("description") or ""
        posts = [p.get("content", "") for p in data.get("post_history", [])]
        
        # Combine all text for this actor
        all_text = desc + " " + " ".join(posts)
        all_text_lower = all_text.lower()
        
        # 1. Tech Stack Extraction
        detected_tech = set()
        for tech, patterns in TECH_STACK_KEYWORDS.items():
            for pat in patterns:
                if re.search(pat, all_text_lower):
                    detected_tech.add(tech)
                    break # Found this tech

        for tech in detected_tech:
            tech_counts[tech] += 1
            
        actor_tech_map[actor] = list(detected_tech)
        
        # 2. Trending Topics Extraction
        # Remove boilerplate
        clean_text = all_text_lower
        for b in BOILERPLATE:
            clean_text = re.sub(b, " ", clean_text)
            
        # Basic tokenization (alphanumeric only)
        words = re.findall(r'\b[a-z]{3,}\b', clean_text)
        
        for w in words:
            if w not in STOP_WORDS and len(w) > 3:
                word_counts[w] += 1

    top_tech = [{"tech": k, "count": v} for k, v in tech_counts.most_common(15)]
    top_topics = [{"topic": k, "count": v} for k, v in word_counts.most_common(30)]
    
    result = {
        "top_tech": top_tech,
        "top_topics": top_topics,
        "actor_tech_map": actor_tech_map
    }
    
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(result, f, indent=2)
        
    # Real append-only Time-Series
    import datetime
    today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    ts_path = OUTPUT_PATH.parent / "tech_timeseries.json"
    ts_data = []
    if ts_path.exists():
        with open(ts_path, "r") as f:
            try:
                ts_data = json.load(f)
            except json.JSONDecodeError:
                pass
                
    # Check if we already have an entry for today (overwrite if yes)
    snapshot = {
        "date": today_str,
        "top_tech": top_tech,
        "top_topics": top_topics[:15] # Keep it smaller for timeseries
    }
    
    found = False
    for item in ts_data:
        if item.get("date") == today_str:
            item.update(snapshot)
            found = True
            break
            
    if not found:
        ts_data.append(snapshot)
        
    with open(ts_path, "w") as f:
        json.dump(ts_data, f, indent=2)
        
    print(f"✅ Content Analytics Complete! Processed {len(files)} agents.")
    print(f"   Top Tech: {[t['tech'] for t in top_tech[:5]]}")
    print(f"   Top Topics: {[t['topic'] for t in top_topics[:5]]}")
    print(f"   Saved to {OUTPUT_PATH} and {ts_path}")

if __name__ == "__main__":
    run_analytics()
