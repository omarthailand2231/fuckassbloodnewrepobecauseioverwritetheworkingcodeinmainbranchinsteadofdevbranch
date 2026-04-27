# Contributing to BloodAI

Thanks for wanting to contribute. Here's how.

## Getting Started

1. Fork the repo
2. Clone your fork: `git clone https://github.com/YOUR_USERNAME/fuckassbloodnewrepobecauseioverwritetheworkingcodeinmainbranchinsteadofdevbranch.git`
3. `cd fuckassbloodnewrepobecauseioverwritetheworkingcodeinmainbranchinsteadofdevbranch/BloodAI`
4. `python3 -m venv venv && source venv/bin/activate`
5. `pip install -r requirements.txt`
6. `cp .env.example .env` — fill in your API keys
7. `bash run.sh` to test

## Making Changes

- Create a branch: `git checkout -b feat/my-feature`
- Make your changes
- Test that the bot starts without errors: `python -c "import py_compile; py_compile.compile('bot.py', doraise=True); py_compile.compile('tools.py', doraise=True)"`
- Commit with a descriptive message
- Push and open a PR

## What to Contribute

- **New tools** — add to `TOOL_DEFINITIONS` in `tools.py` + execution handler in `execute_tool`
- **New commands** — add `@bot.hybrid_command` in `bot.py`
- **Bug fixes** — always welcome
- **Skills** — write `.md` files in `skills/` that help Blood solve specific problems
- **Persona modes** — new persona overrides like Trump mode (see `_persona_overrides` in `bot.py`)
- **Memory improvements** — better vector search, smarter compaction

## Rules

- **Never commit `.env`** — it has real API keys
- **Never hardcode secrets** — use `os.getenv()` or `CONFIG`
- **Don't break existing behavior** — Blood's personality is sacred
- **Test before PR** — at minimum, verify the bot starts and responds to `@blood hello`
- **Keep it concise** — Blood hates verbosity. So do we.

## Code Style

- Python 3.11+
- No type-checking enforcement but type hints are appreciated
- Follow the existing patterns in each file
- Comments are fine but don't over-document

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
