const { mergePartialPythonUpdate } = require('../shared/utils/sovereign_merger');
const crypto = require('crypto');

async function runTest() {
    const existing = `
def hello():
    print("Old Logic")

def stay():
    print("Don't touch me")
`;

    const emitted = `
def hello():
    print("New Logic 🦞")
`;

    const baseHash = crypto.createHash('sha256').update(existing).digest('hex');

    console.log("--- Test 1: Successful Merge ---");
    try {
        const merged = await mergePartialPythonUpdate(existing, emitted, baseHash);
        console.log("Result:\n", merged);
    } catch (e) {
        console.error("Test 1 Failed:", e.message);
    }

    console.log("\n--- Test 2: Conflict (Hash Mismatch) ---");
    try {
        // Pass a wrong hash
        await mergePartialPythonUpdate(existing, emitted, "wrong_hash");
        console.error("Test 2 Failed: Should have thrown a conflict error!");
    } catch (e) {
        console.log("Caught expected error:", e.message);
        if (e.code === 'CONFLICT') {
            console.log("Correctly identified as CONFLICT.");
        }
    }

    console.log("\n--- Test 3: Syntax Error ---");
    try {
        await mergePartialPythonUpdate(existing, "def broken(:", baseHash);
        console.error("Test 3 Failed: Should have thrown a merge error!");
    } catch (e) {
        console.log("Caught expected error:", e.message);
    }
}

runTest();
