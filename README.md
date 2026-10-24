# NaoNao Platform Adapter for Hermes

NaoNao is a community and channel-based communication platform. This adapter connects a Hermes agent you run yourself to NaoNao channels through the NaoNao Public Bot API and Gateway.

**Plugin version: 1.1.0**

Hermes replies when someone @mentions your NaoNao bot. You configure one secret, the NaoNao Bot Token. NaoNao does not host Hermes, store your model API key, or pay model usage.

Text messages only. When Message Edit is available on the NaoNao Public Bot API, Hermes streams by creating one channel message and PATCHing the same `message.id` until generation finishes.

## What it does

- Receives `message.bot_mention` and `command.invoked` on the Public Bot API v1 Gateway.
- Sends text replies with `POST /api/v1/channels/:channelId/messages`.
- Streams replies with `PATCH /api/v1/channels/:channelId/messages/:messageId` on the same message id (`SUPPORTS_MESSAGE_EDITING = True`).
- Supports ACK, resume, event dedupe, and outbound `Idempotency-Key` on create.
- Optional Home Channel for `hermes send`, cron, and other proactive delivery.

## Requirements

- A NaoNao account
- Hermes Agent, with the `hermes` command on your PATH
- This plugin installed into your Hermes home
- Hermes **streaming enabled** for same-message progressive updates (see below)

A production bot needs one value:

```text
NAONAO_BOT_TOKEN
```

`NAONAO_API_BASE` is optional. It defaults to `https://naonaoai.com`.

## Create a NaoNao bot

Copy the token immediately. It is shown once.

Community owners, for one community:

1. Open the community, then 社区设置 → 应用与集成 → 增加机器人.
2. Enter a bot name and create it.
3. Save the Bot Token.

Developers who need an Application, commands, publishing, or a team:

1. Open 开发者中心 → 创建应用 → 创建 Bot.
2. Install the application into a community you manage. Grant 查看频道 and 发送消息 on one text channel.
3. Save the Bot Token.

On that bot, set 运行方式 to 外部机器人. That is the mode this plugin can own. If the bot is still an AI Agent, or external replies are disabled, Hermes will not connect.

## Install

This adapter is not bundled with Hermes. `hermes plugins install naonao` works only after Hermes maintainers list it in the plugin catalog. Install from the public repository:

```bash
hermes plugins install https://github.com/0xzeshawn/hermes-naonao-plugin.git
hermes plugins enable naonao
```

Hermes clones the repository to `~/.hermes/plugins/naonao`. The plugin id is `naonao`. The gateway platform key is also `naonao`.

Local development, when you already have this directory and are not installing from GitHub, can symlink it under the platforms category instead:

```bash
mkdir -p ~/.hermes/plugins/platforms
ln -s /path/to/this/plugin ~/.hermes/plugins/platforms/naonao
hermes plugins enable platforms/naonao
```

That path-derived id is `platforms/naonao`. It is not the id produced by `hermes plugins install`.

Add this to `~/.hermes/config.yaml` if it is not already there:

```yaml
platforms:
  naonao:
    enabled: true
```

## Upgrade from v1.0.1

v1.0.1 shipped with `SUPPORTS_MESSAGE_EDITING = False` (complete messages only). v1.1.0 restores same-message streaming via Bot Message Edit.

Update the installed plugin, then restart the gateway:

```bash
hermes plugins update naonao
hermes gateway restart
```

If `hermes plugins update naonao` is unavailable for your install, reinstall from the public repository:

```bash
hermes plugins remove naonao
hermes plugins install https://github.com/0xzeshawn/hermes-naonao-plugin.git
hermes plugins enable naonao
hermes gateway restart
```

Symlink installs (`platforms/naonao`) must pull or replace the linked directory yourself, then `hermes gateway restart`.

## Enable Hermes streaming

Hermes keeps platform streaming **off by default**. Same-message NaoNao streaming requires the master switch:

```bash
hermes config set streaming.enabled true
hermes gateway restart
```

Equivalent YAML in `~/.hermes/config.yaml`:

```yaml
streaming:
  enabled: true
```

The default transport (`auto`) is enough for NaoNao: this adapter uses progressive `edit_message` (PATCH). You do not need to set `transport: edit` unless you are debugging other platforms.

Without `streaming.enabled: true`, Hermes waits for the full reply and posts once — even with plugin v1.1.0.

## Configure

Save the token with the Hermes CLI. It writes `~/.hermes/.env`. Do not put the token in `config.yaml`, a shell history you share, a URL, or a screenshot.

```bash
hermes config set NAONAO_BOT_TOKEN nn_bot_xxxxxxxxxxxxxxxxx
```

Replace `nn_bot_xxxxxxxxxxxxxxxxx` with the token you just copied.

## Start Hermes

```bash
hermes gateway start
hermes gateway status
```

## Verify install (no Bot Token)

```bash
# Plugin version from the installed tree
python3 - <<'PY'
from pathlib import Path
import re
cands = [
    Path.home() / ".hermes/plugins/naonao",
    Path.home() / ".hermes/plugins/platforms/naonao",
]
for root in cands:
    py = root / "plugin.yaml"
    ad = root / "adapter.py"
    if not py.exists() and not ad.exists():
        continue
    ver = re.search(r"(?m)^version:\s*([^\s#]+)", py.read_text()) if py.exists() else None
    text = ad.read_text() if ad.exists() else ""
    m = re.search(r"SUPPORTS_MESSAGE_EDITING\s*=\s*(\w+)", text)
    print("path=", root.resolve() if root.exists() else root)
    print("version=", ver.group(1) if ver else "MISSING")
    print("SUPPORTS_MESSAGE_EDITING=", m.group(1) if m else "MISSING")
    print("edit_message=", "def edit_message" in text)
PY

hermes config get streaming.enabled
```

Expect: `version=1.1.0`, `SUPPORTS_MESSAGE_EDITING=True`, `edit_message=True`, `streaming.enabled=true`.

## Test with @Bot

In an allowed NaoNao text channel, type `@`, choose the bot from the menu, and send a message. You should get one text reply.

With streaming enabled, a longer answer should appear as one message that grows in place (PATCH), not as a second final message.

The first reply can include a Hermes note that no home channel is set. That note is normal. Chat still works.

## Access control

Hermes decides who may talk to the agent. NaoNao channels are group chats, so Hermes DM pairing does not apply. A sender with no stable NaoNao user id is ignored.

Private Hermes (only specific people):

```bash
hermes config set NAONAO_ALLOW_ALL_USERS true
```

Restart the gateway, then in NaoNao send `/whoami` to the bot. Hermes replies with your platform user id. Copy that id, then replace open access with an allowlist:

```bash
hermes config set NAONAO_ALLOWED_USERS the_user_id_from_whoami
hermes config set NAONAO_ALLOW_ALL_USERS false
```

`/whoami` only runs after Hermes has already admitted you, which is why the temporary allow-all step comes first. NaoNao does not show this id in the product UI. `NAONAO_ALLOWED_USERS` is a comma-separated list of those stable ids.

Community Hermes (members who can @ the bot):

```bash
hermes config set NAONAO_ALLOW_ALL_USERS true
```

This is an explicit opt-in. Anyone who can mention the bot can reach your agent and its tools. Model usage is paid by the person running Hermes.

If neither variable is set, Hermes denies everyone. The bot stays quiet. Hermes does not post "you are not allowed" in the channel.

## Home Channel

`/sethome` is optional. An ordinary `@` mention does not need a home channel.

`/sethome` saves the current NaoNao channel as the place Hermes sends messages that did not start in a channel: cron jobs, messages from another platform, and `hermes send`. Hermes stores one home channel per profile for NaoNao. A later `/sethome` replaces it.

Register the command once, in 开发者中心, on this application. The community “增加机器人” path does not create commands. Hermes does not create the command for you.

```text
Command Name: sethome
Description: Set this channel as the Hermes home channel
Options: none
```

In the channel, choose `/sethome` from the slash menu and send it. A typed slash that was not selected from the menu is ordinary text and does not run the command.

Hermes writes the result itself. Do not set `NAONAO_HOME_CHANNEL` by hand.

## Proactive send

After `/sethome`:

```bash
hermes send --to naonao "hello"
```

The message arrives in the home channel. Cron delivery for this platform uses the same home channel.

## Current limitations

| Feature | Status |
|---|---|
| Text @mentions | Supported |
| Application commands, including `/sethome` once you create it | Supported |
| Home channel | Supported |
| Proactive `hermes send` and cron | Supported |
| Same-message streaming (requires `streaming.enabled: true`) | Supported (v1.1.0+) |
| Images, files, voice, video | Not currently supported |
| Ordinary channel messages that do not @ the bot | Not delivered |

- One Hermes profile has one NaoNao home channel.
- The same bot can be installed in more than one community. Each channel is its own conversation.

## Troubleshooting

| What you see | What to do |
|---|---|
| Log says `NAONAO_BOT_TOKEN is required` | Set the token, then start the gateway again. Hermes does not reconnect in a loop for a missing token. |
| `NaoNao Bot Token is no longer valid` | The token was revoked or is wrong. Create a new one where you manage that bot and update `NAONAO_BOT_TOKEN`. Restart the gateway. |
| `NaoNao Bot is not currently assigned to an external connector` | In the Bot's 运行方式, choose 启用外部机器人. An AI Agent bot will not connect. |
| The bot never replies, and the gateway log shows the user is unauthorized | Set `NAONAO_ALLOWED_USERS` or `NAONAO_ALLOW_ALL_USERS` as above. |
| Sending fails with a permissions error | Confirm the app is installed in that community and has 查看频道 and 发送消息 on that channel. |
| Reply appears all at once (no in-place growth) | Confirm plugin `version: 1.1.0`, `SUPPORTS_MESSAGE_EDITING=True`, and `streaming.enabled=true`, then `hermes gateway restart`. |
| `/sethome` does not appear in the menu | Create the `sethome` command on this application. It is not built in. |
| `hermes` says it cannot write `~/.local/bin` | That is the Hermes CLI symlink, not this plugin. Use a `hermes` binary you can already run, or fix the Hermes install permissions. |

## Configuration reference

| Variable | Required | Default | Set it yourself? |
|---|---|---|---|
| `NAONAO_BOT_TOKEN` | yes | none | yes, in `~/.hermes/.env` |
| `NAONAO_API_BASE` | no | `https://naonaoai.com` | only for a non-production API |
| `NAONAO_ALLOWED_USERS` | no | empty | yes, for a private bot |
| `NAONAO_ALLOW_ALL_USERS` | no | false | yes, for a community bot |
| `NAONAO_HOME_CHANNEL` | no | unset | no; `/sethome` writes it |

Hermes streaming (config.yaml, not env):

| Key | Required for streaming | Default |
|---|---|---|
| `streaming.enabled` | yes | `false` |

Recommended path: `hermes config set` for every value you do set. Alternative: edit `~/.hermes/.env` / `config.yaml` directly. Do not commit either file.

## Integration contract

The adapter uses Public Bot API v1. Production origin is `https://naonaoai.com`. Auth header is `Authorization: Bot <token>`.

```text
GET   /api/v1/bot/me
POST  /api/v1/bot/gateway-session
WSS   /gateway/v1
POST  /api/v1/channels/:channelId/messages
PATCH /api/v1/channels/:channelId/messages/:messageId
```

NaoNao owns the Bot Token, REST API, Gateway, installation, permissions, and the `message.bot_mention` and `command.invoked` events. Hermes owns agent execution, authorization, Home Channel, `/sethome`, `/whoami`, cron, and cross-platform delivery. The adapter translates between them.

## Maintainer note (drift)

This GitHub repository is what Hermes users install. If you also maintain a copy under an application monorepo, sync Hermes adapter changes here and bump `plugin.yaml` before release. Do not ship a capability flag mismatch (for example `SUPPORTS_MESSAGE_EDITING` True in one tree and False in the other).

## Documentation

- [NaoNao Hermes guide](https://naonaoai.com/docs/hermes)
- [Public Bot API v1](https://naonaoai.com/docs/public-bot-api)

## Security

The Bot Token is a secret. Do not commit it, put it in a URL, log it, share it publicly, or share a screenshot that contains it. The plaintext is shown once, at creation or rotation. If it is exposed, revoke it and set the new value with `hermes config set`.

## License

MIT License. The license text is the `LICENSE` file in [hermes-naonao-plugin](https://github.com/0xzeshawn/hermes-naonao-plugin).
