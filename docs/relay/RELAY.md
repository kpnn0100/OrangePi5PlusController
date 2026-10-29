# Device manager: reach your machines over the internet (RLY-01..26)

A machine running Arstro Remote sits at home or in a lab, behind NAT. The **device manager**
(a small server in a Docker container, e.g. on an Oracle Cloud VM) is on the internet. Each
machine **advertises** itself to it over one outbound connection. You open
`https://<server>/arstro/internal/devices/`, pick a machine, type its **access key**, pick a
**port**, and that port's page (`127.0.0.1:<port>` on the machine) opens in your browser. Any
port works: 8080 is Arstro Remote itself, others are whatever web apps run there. Agents and
scripts use the same path through a small Python client that can also forward any port
(SSH included) to a local one ([AGENT_API.md](AGENT_API.md)).

Everything between you and the machine is **end-to-end encrypted with the access key**. The
device manager forwards ciphertext and never has the key. Wire format: [PROTOCOL.md](PROTOCOL.md).

**Behind any web proxy.** The connections are WebSockets when the path allows them. When a
proxy does not pass WebSockets (e.g. an nginx without the `Upgrade` lines), the device, the
client and the browser switch by themselves to an HTTP transport - a waiting GET brings messages
down, small POSTs take them up - carrying the very same encrypted messages (RLY-27,
PROTOCOL.md section 6). A plain `proxy_pass` is then enough; WebSockets are just faster.

```
 you (browser / agent)                 device manager (Docker)                      machine (behind NAT)
 holds the ACCESS KEY                  https://<server>/arstro/internal/devices     arstro-remote, module relay
                                                                                    holds the ACCESS KEY
 B/ device list  ───── HTTPS ────────► lists machines that advertise ◄──── WS B/api/agent (outbound, kept open)
 B/d/<id>/<port>/ ── WS B/api/connect ─► joins viewer ⇄ machine ◄────────── WS B/api/agent/data (one per viewer)
      ◄══════════════ AES-256-GCM, keys from the access key (HKDF), relay sees only ciphertext ═════════════►
                                                                                     │
                                                                                     ▼ 127.0.0.1:<port>
```

## Setting it up

1. **Device manager** (once). On the server, from a checkout of this repo:
   `DOMAIN=abc.example.com docker compose -f relay/docker-compose.yml up -d` (Caddy gets the
   HTTPS certificate), or `docker run` the image behind your own HTTPS proxy. Open 80/443 in
   the VCN security list **and** the VM firewall. `docker logs` shows the **join token**.
   Operator guide: [relay/README.md](../../relay/README.md). For someone who only has Docker,
   `relay/package.sh --arm64-host <arm64 machine>` makes
   `release/arstro-devices-<version>.tar.gz` (images for amd64 and arm64, compose with Caddy,
   `run.sh`, a README for them): they unpack it, set `DOMAIN` and run `./run.sh`.
2. **Each machine.** `install.sh --relay https://abc.example.com/arstro/internal/devices
   --relay-token <join token>`, or the web UI's *Internet access* page, or `arstro-remote relay
   set --url ... --token ... --enable`. The installer prints the **access key**; later
   `arstro-remote relay key` or the page shows it again. Each slot is its own device (own id
   and key); usually one slot per machine is enough.
3. **Use it.** Open the device manager in a browser, open the machine, paste the key, pick a
   port. Or use the client: `arstro-remote relay forward <id> 8080 --listen 127.0.0.1:18080`.

## The access key

* 32 random bytes made by the machine on its first start (`ak1_` + 43 characters), kept 0600
  in the slot's config dir. Whoever has it can reach **every port** of that machine (unless
  the slot has a port allow-list) - give it only to people and agents that may do that.
* It never travels: the handshake proves that both sides have it (HMAC proofs, the machine's
  first, so a fake machine learns nothing) and derives fresh session keys per channel.
* Replace it with *Replace key* / `relay rotate`: every open viewer is disconnected and the
  old key stops working at once.
* Browsers keep it as a **non-extractable** WebCrypto key in IndexedDB (remembered, or for
  12 h); scripts can read it from a file or `ARSTRO_RELAY_KEY`.

## Security model

| Party | Trusted for | Not trusted for |
|---|---|---|
| The machine | everything | - |
| Holder of the access key | reaching the machine's ports (then each app's own login: e.g. Arstro Remote still asks its password) | - |
| The device manager / its server / its operator | keeping the list and the pipe up | reading or changing traffic, using a machine |
| The internet | nothing | everything |

What the device manager **sees**: which machines exist and are online, their names, host
names, versions and the Arstro web port hint, viewer IP addresses, when channels open, and
how many bytes pass. What it **cannot** do: read or change what passes (AES-GCM, keys from the
access key it never gets), replay old traffic (fresh nonces per channel, counters per frame),
pretend to be a machine (it cannot produce the machine's proof), or let another machine take
an id (ids are bound to a secret at first registration). It can always **refuse service**.

**The one thing to know about the browser viewer:** the pages that do the encryption
(`index.html`, `app.js`, `boot.html`, `boot.js`, `sw.js`, `shim.js`, `viewer.js`, `e2e.js`) are
served by the device manager. An operator who
deliberately changes those files could make your browser send the key it types to them. The
encryption therefore protects against a careless, hacked-but-unmodified or merely curious
server (logs, disks, traffic dumps, other tenants), not against an operator who rewrites the
viewer. If that matters: run the device manager yourself from this repo's image, or use the
Python client from your own checkout of this repo (it loads no code from the server), and
replace a key you think was exposed.

Other limits of the browser viewer: every machine's pages share the device manager's origin,
so a page of one machine could *use* (not read) the keys this browser holds for other
machines; apps that need their own service worker, or cookies readable by JavaScript, do not
work through it - use `forward` for those.

| Attack | What stops it |
|---|---|
| Someone finds the device list | it shows names only; nothing works without a machine's key; set `RELAY_PASSWORD` to hide the list too |
| Guessing a key | 256-bit keys; five wrong proofs refuse new channels for a minute; every wrong key is logged |
| A fake machine with the same name | it cannot produce the machine's proof: the viewer stops before sending anything derived from the key |
| Registering junk machines / taking an id | join token required; id bound to its secret |
| Reading or changing traffic on the server | end-to-end AES-GCM |
| Cross-site requests into a machine's app | the machine's apps keep their own checks; the device manager's pages use a strict CSP |
| Leaked key | replace it; the machine closes every viewer |
| Root on the machine | out of scope: it owns everything anyway |

## Where things are

| Part | Path |
|---|---|
| Protocol crypto, framing, message names (one definition) | `server/arstro_remote/relay/e2e.py` |
| Machine side: tunnel, channels, local proxy | `server/arstro_remote/relay/agent.py` |
| Machine side: ops `relay.*`, topic `relay`, identity, settings | `server/arstro_remote/relay/service.py` |
| Standalone client (agents, `arstro-remote relay devices/ports/http/forward`) | `server/arstro_remote/relay/client.py` (+ `e2e.py`, `web/ws.py`) |
| Device manager server, admin CLI | `relay/arstro_relay/` |
| Browser viewer: list, key, port (`index.html`, `app.js`, `style.css`), boot page (`boot.html`, `boot.js`), service worker (`sw.js`), WebSocket and link shim (`shim.js`), routing / cookie jar / key store shared by all of them (`viewer.js`), WebCrypto twin of `e2e.py` (`e2e.js`) | `relay/arstro_relay/static/` |
| Docker image, compose with Caddy, operator guide | `relay/Dockerfile`, `relay/docker-compose.yml`, `relay/Caddyfile`, `relay/README.md` |
| Tests | `server/tests/test_relay.py`, `server/tests/test_relay_docs.py`, `relay/tests/` |

Changing any of it: the `arstro.device_manager.implement` skill (keeps code, protocol,
vectors and every document in sync).
