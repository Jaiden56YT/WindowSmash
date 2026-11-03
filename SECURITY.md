# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue, pull request or Discord channel.

1. Go to the repository's **[Security](https://github.com/OpenSmash-Ultimate/OpenSmash-Ultimate/security)** tab and choose **Report a vulnerability**. This opens a private advisory that only you and the maintainers can see.
2. If that button isn't there, open a public issue titled "Security contact request" with no details, and a maintainer will reply with a private way to send them.

Say what the problem is, where it is (file and line, or the server and port), how to reproduce it with made-up data, and what an attacker could do with it. Name the commit or release you tested.

> [!WARNING]
> **Never attach game files, keys, firmware, saves or generated game code**, not even privately and not to prove a point. A report must reproduce without them. Strip logs and crash reports of your folder names, player names, account IDs and internet addresses first. If a problem can only be shown with game data, describe it in words and we will reproduce it ourselves.

## What counts

Anything that could harm a player or their computer, for example:

- code in the runtime or the tools that a crafted network packet, save, mod or game file can make misbehave;
- the installers and self-updaters: what they download and run, and how they check it;
- the online services: the lobby, the relay and the game server (NEX), and anything they log or expose about players;
- a way for this repository, its CI or its releases to end up holding game data or secrets.

This covers the code, CI and releases of this repository and, while players still install from them, of the WindowSmash and ssbu-recomp repositories in the same organization. It also covers the public lobby and game servers the project's maintainers run. Problems in services the installers, builds or CI download from (GitHub, MSYS2, eden, Microsoft and others) belong to those services: please report them there.

## What happens next

A maintainer replies in the private advisory, works on a fix there, and credits you when the fix is published, unless you'd rather not be named. Please give us a reasonable time to fix it before you talk about it publicly.
