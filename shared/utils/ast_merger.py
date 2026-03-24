import ast
import logging
import hashlib
from typing import Any

logger = logging.getLogger(__name__)

def compute_content_hash(content: str) -> str:
    """Compute a SHA-256 hash of the content for concurrency guarding."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()

def merge_partial_python_update(
    existing_content: str,
    emitted_content: str,
    expected_base_hash: str | None = None
) -> str | None:
    """
    Merge top-level Python definitions (functions/classes) from emitted into existing.
    
    If expected_base_hash is provided, the merge will fail if the existing_content
    hash does not match, ensuring concurrency safety in multi-agent environments.
    """
    if expected_base_hash:
        current_hash = compute_content_hash(existing_content)
        if current_hash != expected_base_hash:
            logger.warning(f"Hash mismatch: current {current_hash} != expected {expected_base_hash}")
            return None

    try:
        existing_tree = ast.parse(existing_content)
        emitted_tree = ast.parse(emitted_content)
    except SyntaxError as e:
        logger.debug(f"Syntax error during AST merge: {e}")
        return None

    # Extract eligible definitions from the emitted content
    emitted_defs = {
        getattr(node, "name", ""): node
        for node in emitted_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    
    if not emitted_defs:
        logger.debug("No mergeable definitions found in emitted content.")
        return None

    replaced = False
    merged_body = []
    
    # Iterate through existing code and replace matching definitions
    for node in existing_tree.body:
        name = getattr(node, "name", "")
        if name and name in emitted_defs:
            merged_body.append(emitted_defs.pop(name))
            replaced = True
            logger.info(f"Merged update for definition: {name}")
        else:
            merged_body.append(node)

    # Note: If there are NEW definitions in emitted_defs that weren't in existing_tree,
    # we could theoretically append them, but for this "rehydration" use case,
    # we only want to replace existing ones. 
    # Current implementation ignores new definitions to stay safe.
    
    if not replaced:
        logger.debug("No matching definitions found to replace in existing content.")
        return None

    # Re-apply merged body to the tree
    existing_tree.body = merged_body
    ast.fix_missing_locations(existing_tree)
    
    try:
        return ast.unparse(existing_tree).strip() + "\n"
    except Exception as e:
        logger.error(f"Error unparsing AST after merge: {e}")
        return None

if __name__ == "__main__":
    import sys
    import json

    # CLI Interface for Node.js Bridge / Sovereign Middleware
    try:
        # Read from stdin
        input_data = json.load(sys.stdin)
        existing = input_data.get("existing", "")
        emitted = input_data.get("emitted", "")
        expected_hash = input_data.get("expected_hash")

        merged = merge_partial_python_update(existing, emitted, expected_hash)
        
        if merged:
            print(json.dumps({"success": True, "merged": merged}))
        else:
            # Check for hash mismatch vs other failures
            current_hash = compute_content_hash(existing)
            if expected_hash and current_hash != expected_hash:
                print(json.dumps({"success": False, "error": "Conflict", "current_hash": current_hash}))
            else:
                print(json.dumps({"success": False, "error": "Merge failed (no definitions replaced or syntax error)."}))
            sys.exit(1)
            
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))
        sys.exit(1)
