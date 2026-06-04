# Ask Human Now

> Deprecated package name for Ask Human.

**This PyPI package is no longer developed.** The project has been renamed to
`ask-human`.

Use the new package instead:

```bash
pip install ask-human
ask-human --help
```

For `uvx` MCP client configuration, use:

```json
{
  "mcpServers": {
    "ask-human": {
      "command": "uvx",
      "args": ["ask-human", "--transport", "stdio"]
    }
  }
}
```

Current project links:

- PyPI: <https://pypi.org/project/ask-human/>
- GitHub: <https://github.com/alexchexes/ask-human>
- Issues: <https://github.com/alexchexes/ask-human/issues>

This final `ask-human-now` release keeps the old package functional for users
who already installed or pinned it, but new installs should use `ask-human`.
The old `ask-human-now` package name may be transferable if someone has a real
need for it; open an issue in the current repository to discuss that.

## Old Package Details

The old distribution name was `ask-human-now`, with Python import package
`ask_human_now` and CLI command `ask-human`.

The old `uvx` shape was:

```bash
uvx --from ask-human-now ask-human --transport stdio
```

That still works for this final package release, but it is retained only so
existing users are not broken.

## Maintenance

This branch exists only for the final deprecated package-name release. For local
maintenance checks:

```bash
pip install -e ".[dev]"
black --check .
isort --check-only .
mypy src
pyright
pytest
```

## License

MIT License. See [LICENSE](LICENSE).

Ask Human is a maintained fork of the original `ask-human-for-context-mcp`
project. The upstream MIT license notice is retained.
