# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@AGENTS.md

## Running a single test

Tests are Django `TestCase`s in `videos/tests.py` (no pytest), run inside the container:

```bash
docker compose exec web python manage.py test videos.tests.<TestClass>
docker compose exec web python manage.py test videos.tests.<TestClass>.<test_method>
```

Domain terms are defined in `GLOSSARY.md`; architecture decisions are in `docs/adr/`.
