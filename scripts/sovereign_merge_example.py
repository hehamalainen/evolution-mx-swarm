import sys
import os
import json

# Add the project root to sys.path to import our utility
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.utils.ast_merger import merge_partial_python_update, compute_content_hash

def demo_sovereign_handshake():
    """
    Demonstrates the high-integrity AST merge protocol.
    """
    
    # 1. THE EXISTING STATE
    existing_code = """
def calculate_reward(score):
    return score * 0.1

def log_event(msg):
    print(f"LOG: {msg}")
"""
    
    # 2. THE EMITTED UPDATE (Selective)
    # The agent only wants to update the 'calculate_reward' function.
    emitted_snippet = """
def calculate_reward(score):
    # Upgraded logic: 5% boost for high scores
    multiplier = 0.15 if score > 100 else 0.1
    return score * multiplier
"""
    
    # 3. CONCURRENCY GUARD
    # Compute the hash of what the agent *thinks* it is editing.
    base_hash = compute_content_hash(existing_code)
    print(f"--- Base Hash: {base_hash} ---")

    # 4. EXECUTE MERGE
    print("\n--- Executing AST Merge ---")
    merged_code = merge_partial_python_update(
        existing_content=existing_code,
        emitted_content=emitted_snippet,
        expected_base_hash=base_hash
    )
    
    if merged_code:
        print("SUCCESS: Partial update applied via AST rewrite.")
        print("\n--- Resulting Code ---")
        print(merged_code)
    else:
        print("FAILURE: Merge rejected (likely hash mismatch or syntax error).")

    # 5. DEMONSTRATE CONFLICT (Mismatched Hash)
    print("\n--- Simulating Conflict (Wrong Hash) ---")
    stale_hash = "stale-hash-value"
    conflict_result = merge_partial_python_update(
        existing_content=existing_code,
        emitted_content=emitted_snippet,
        expected_base_hash=stale_hash
    )
    
    if not conflict_result:
        print("VERIFIED: Guard correctly rejected update to modified file.")

if __name__ == "__main__":
    demo_sovereign_handshake()
