# Maple Next

Maple Next is a **human-operated battle-assistance application for Pokémon Champions**.
Its first product goal is to complete one Battle-1 match through this flow:

```text
UGREEN capture
→ OCR candidates
→ human confirmation
→ Gemini advice
→ human game operation
→ human MOVE / SWITCH record
→ human WIN / LOSE record
→ local JSON save
```

Maple never operates the game automatically. Gemini send, Selection APPLY, MOVE / SWITCH,
and WIN / LOSE are always explicit human actions.

Maple Next does not inherit the old Flask, browser, or localhost runtime. Analytics dashboards
and 60-match evaluation infrastructure must not be implemented ahead of the Battle-1 flow.

## Issue #23 scope

This candidate implements only the mock foundation:

- pure Python BattleSession domain model
- canonical battle types and HP buckets
- immutable worker Job / Result envelopes
- SQLite single-writer repository and migration
- provider non-replay restart recovery
- Domain Projection for CTA rendering
- synthetic Selection → APPLY → BATTLE_READY → restart tests

It does **not** implement PySide6 screens, real OCR, UGREEN access, provider network calls,
API-key handling, automatic retry, model fallback, or game automation.

## Development

Python 3.11 is the production target.

```bash
python -m pip install -e '.[dev]'
pytest
ruff check .
mypy src
```
