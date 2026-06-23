# ExpenseIQ — Antigravity Persistent Rules

## Core Rules
- Always use model: gemini-2.5-flash
- Never hardcode API keys — always use os.environ or .env
- Run `uv run pytest tests/ -v` after every code change
- All expenses processed through security_checkpoint before LLM
- Use `uv run` prefix for all Python commands

## Pre-commit Remediation Loop
If a pre-commit hook fails:
1. Read the exact error message
2. Fix the specific issue
3. Re-run the failing hook manually to verify fix
4. Stage the fix and commit again
Do NOT skip hooks with --no-verify

## Code Standards
- Type hints on all function parameters
- Docstrings on all nodes and tools
- No broad except clauses that catch BaseException
- Import dotenv and call load_dotenv() at top of every entry point

## Security Standards
- PII redaction must happen BEFORE any LLM call
- Injection detection must happen BEFORE any LLM call  
- Risk score >= 0.80 routes to escalation, never to LLM
- All regex patterns in security.py, never inline