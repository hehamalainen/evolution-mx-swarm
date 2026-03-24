const { spawn } = require('child_process');
const path = require('path');

/**
 * Node.js bridge for the Sovereign AST Merger (Python).
 * 
 * Enables Node.js platforms (like AgentCommerceOS) to leverage
 * AST-based partial Python updates with Base-Hash Guards.
 */
function mergePartialPythonUpdate(existing, emitted, expectedHash = null) {
    return new Promise((resolve, reject) => {
        const pythonScript = path.join(__dirname, 'ast_merger.py');
        const pythonProcess = spawn('python3', [pythonScript]);

        let stdout = '';
        let stderr = '';

        pythonProcess.stdout.on('data', (data) => {
            stdout += data.toString();
        });

        pythonProcess.stderr.on('data', (data) => {
            stderr += data.toString();
        });

        pythonProcess.on('close', (code) => {
            try {
                const result = JSON.parse(stdout);
                if (code === 0 && result.success) {
                    resolve(result.merged);
                } else {
                    const error = result.error || stderr || 'Unknown error';
                    if (error === 'Conflict') {
                        const conflictErr = new Error('Merge Conflict: Base hash mismatch.');
                        conflictErr.code = 'CONFLICT';
                        conflictErr.currentHash = result.current_hash;
                        reject(conflictErr);
                    } else {
                        reject(new Error(`Sovereign Merger failed: ${error}`));
                    }
                }
            } catch (e) {
                reject(new Error(`Failed to parse merger output: ${e.message}. Raw: ${stdout}`));
            }
        });

        // Send input as JSON to stdin
        const input = JSON.stringify({
            existing: existing,
            emitted: emitted,
            expected_hash: expectedHash
        });

        pythonProcess.stdin.write(input);
        pythonProcess.stdin.end();
    });
}

module.exports = {
    mergePartialPythonUpdate
};
