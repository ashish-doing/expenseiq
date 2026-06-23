---
name: expense-validator
description: Deterministically validates expense submissions before agent processing
version: 1.0.0
level: 4
---

# Expense Validator Skill

## Purpose
Validate expense fields deterministically using a Python script — no LLM judgment.
This ensures binary-correct validation before the agent graph runs.

## When to Use
Use this skill when asked to validate an expense submission or check if an expense
meets policy requirements before routing to the agent.

## Steps
1. Extract expense fields from the user's input
2. Run the validation script:
```
   python .agents/skills/expense-validator/scripts/validate_expense.py '<json>'
```
3. If exit code 0: expense is valid, proceed to agent
4. If exit code 1: report the specific validation error to the user

## Validation Rules
- amount must be > 0
- category must be one of: meals, travel, software, hardware, training, office
- description must not be empty
- date must be valid ISO format (YYYY-MM-DD)
- submitter must be a valid email format