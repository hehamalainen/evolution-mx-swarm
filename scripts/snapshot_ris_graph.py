import shutil
import sqlite3
import json
from pathlib import Path
from datetime import datetime

# Paths
SOURCE_DB_PATH = Path("<REDACTED_LOCAL_PATH>/Downloads/epstein-archive-analyst/data/moltbook_analytics.db")
SNAPSHOTS_DIR = Path("<REDACTED_LOCAL_PATH>/Downloads/epstein-archive-analyst/data/snapshots")

def snapshot_graph():
    if not SOURCE_DB_PATH.exists():
        print(f"❌ Source database not found at {SOURCE_DB_PATH}")
        return

    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    snapshot_db_path = SNAPSHOTS_DIR / f"ris_snapshot_{timestamp}.db"
    snapshot_json_path = SNAPSHOTS_DIR / f"ris_snapshot_{timestamp}.json"
    
    # 1. Copy the raw database
    print(f"📸 Copying database to {snapshot_db_path}...")
    shutil.copy2(SOURCE_DB_PATH, snapshot_db_path)
    
    # 2. Extract a lightweight JSON representation of the graph topology for fast simulation loading
    conn = sqlite3.connect(SOURCE_DB_PATH)
    conn.row_factory = sqlite3.Row
    
    nodes = []
    edges = []
    
    try:
        # Extract nodes
        node_rows = conn.execute("SELECT canonical_entity, mentions FROM entity_resolution WHERE entity_type = 'PERSON'").fetchall()
        for row in node_rows:
            nodes.append({
                "id": str(row["canonical_entity"]),
                "mentions": int(row["mentions"] or 0)
            })
            
        # Extract edges
        edge_rows = conn.execute("SELECT source_entity, target_entity, count(*) as weight FROM relation_triples GROUP BY source_entity, target_entity").fetchall()
        for row in edge_rows:
            edges.append({
                "source": str(row["source_entity"]),
                "target": str(row["target_entity"]),
                "weight": int(row["weight"] or 0)
            })
            
        topology = {
            "timestamp": timestamp,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "nodes": nodes,
            "edges": edges
        }
        
        with open(snapshot_json_path, "w") as f:
            json.dump(topology, f, indent=2)
            
        print(f"✅ Extracted topology to {snapshot_json_path} (Nodes: {len(nodes)}, Edges: {len(edges)})")
        
    except sqlite3.Error as e:
        print(f"⚠️ SQL Error during JSON extraction: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    snapshot_graph()
