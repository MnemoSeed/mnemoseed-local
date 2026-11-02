# MnemoSeed Local

**Local memory for your coding agents.**

MnemoSeed Local is a single-user memory layer that runs on your machine. It
captures conversations, retrieves relevant context across sessions, and provides
a local web console. Adapters are available for OpenCode, Claude Code, ZCode,
and Codex CLI.

[Website](https://mnemoseed.my) · [Complete guide](docs/guide.md) ·
[GitHub Sponsors](https://github.com/sponsors/MnemoSeed)

## Install

The current install route is a source checkout. The package is not yet published
on PyPI, so the package-index installers are currently unavailable.

Prepare [Git](https://git-scm.com/downloads),
[uv](https://docs.astral.sh/uv/getting-started/installation/), and Python 3.12 or
newer (uv can provision Python). Then run:

```sh
git clone https://github.com/MnemoSeed/mnemoseed-local.git
cd mnemoseed-local
uv tool install --python 3.12 --force ".[codex]"
mnemoseed-local init --mode capture-recall
mnemoseed-local doctor
```

Capture uses a local embedding model whose assets may need an initial download;
resolve readiness failures reported by `doctor`.

Start `mnemoseed-local up` in a separate terminal, then follow the
[host setup and verification steps](docs/guide.md#host-setup). Installation alone
does not connect your coding agent or configure startup at login.

This setup keeps automatic dream off. Capture and recall can run without Ollama.
For manual dream consolidation,
[install Ollama and prepare the configured model](docs/guide.md#dream-model-setup).
The default dream route uses `qwen3.5:9b`; hardware requirements depend on the model.

## Supported hosts

| Host | Setup |
| --- | --- |
| OpenCode | [Plugin setup](docs/guide.md#opencode) |
| Claude Code | [Hook setup](docs/guide.md#claude-code) |
| ZCode | [Host setup](docs/guide.md#zcode) |
| Codex CLI | [Host setup and trust review](docs/guide.md#codex-cli) |

Connect only the hosts you choose with the [host commands](docs/guide.md#host-setup).
Host coverage varies by platform. Codex Desktop app-server mode is unverified.

## Documentation and support

- [Complete guide](docs/guide.md): setup, memory behavior, configuration, and troubleshooting.
- [Uninstall and data removal options](docs/guide.md#uninstall).
- [Memory Atlas verification](features/atlas.md).
- [Chinese documentation](docs/zh/README.md).
- [Report an issue](https://github.com/MnemoSeed/mnemoseed-local/issues).
- [Contact](mailto:hello@mnemoseed.my) · [Website](https://mnemoseed.my).

## What it does NOT do yet

Memory is stored in plaintext; there is no encryption-at-rest guarantee.
Dream inference uses a local model by default, but a remote endpoint can be
configured and receives the data sent to it. Multi-device sync and a hosted
cloud daemon are outside this edition. Experience learning remains unfinished
and off; dream currently produces structured facts. Windows logon startup,
when registered, does not recover the daemon after a crash.

## Sponsor MnemoSeed

Help support continued development through [GitHub Sponsors](https://github.com/sponsors/MnemoSeed).

## License

[MIT](LICENSE).

<details>
<summary>Technical reference and previous README links</summary>

## Status

See the [current capabilities and limits](docs/guide.md#status).

## Feature verification

See [feature verification](docs/guide.md#feature-verification).

### Claude Code

See [Claude Code setup](docs/guide.md#claude-code).

## Uninstall

See [adapter removal, data retention, and package removal](docs/guide.md#uninstall).

## MCP gateway

See [MCP tools and connection details](docs/guide.md#mcp-gateway).

## Agent skill

See the [optional agent skill](docs/guide.md#agent-skill).

## Daemon credential

See the [credential boundary and threat model](docs/guide.md#daemon-credential).

## Daemon lifecycle

See [startup, shutdown, and recovery](docs/guide.md#daemon-lifecycle).

## Development

See the [development checks](docs/guide.md#development).

## Evaluation (maintainers)

See the [evaluation harness](docs/guide.md#evaluation-maintainers).

</details>
