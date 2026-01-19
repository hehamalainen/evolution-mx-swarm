# PRD: Simple Calculator API

## Overview
Build a simple REST API calculator service that can perform basic math operations.

## Goals
1. Create a working HTTP API for basic calculations
2. Include proper error handling
3. Add comprehensive tests

## User Stories
- As a developer, I want to call an API endpoint to add two numbers
- As a developer, I want to call an API endpoint to subtract two numbers
- As a developer, I want to call an API endpoint to multiply two numbers
- As a developer, I want to call an API endpoint to divide two numbers (with zero handling)

## Requirements

### Functional Requirements
1. **FR-1: Addition Endpoint**
   - POST /calculate/add with JSON body {"a": number, "b": number}
   - Returns {"result": number}

2. **FR-2: Subtraction Endpoint**
   - POST /calculate/subtract with JSON body {"a": number, "b": number}
   - Returns {"result": number}

3. **FR-3: Multiplication Endpoint**
   - POST /calculate/multiply with JSON body {"a": number, "b": number}
   - Returns {"result": number}

4. **FR-4: Division Endpoint**
   - POST /calculate/divide with JSON body {"a": number, "b": number}
   - Returns {"result": number}
   - Returns 400 error if b is zero

5. **FR-5: Health Check**
   - GET /health returns {"status": "ok"}

### Non-Functional Requirements
1. Response time < 100ms
2. Input validation on all endpoints
3. Proper HTTP status codes

## Technical Stack
- Python 3.11+
- FastAPI
- Pytest for testing

## Definition of Done
- [ ] All 5 endpoints implemented
- [ ] All endpoints have input validation
- [ ] Division by zero returns proper error
- [ ] Unit tests for all operations
- [ ] Integration tests for all endpoints
- [ ] Tests achieve >90% coverage
