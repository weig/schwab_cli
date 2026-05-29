# `cert`

Manage the local TLS certificate used by the OAuth callback server. The
`local_server` [auth flow](auth.md) runs a tiny HTTPS server on `127.0.0.1`
to receive Schwab's redirect; for the browser to trust that server without a
security warning, a one-time local root CA must be trusted by the OS.

**macOS only** for `install` / `uninstall` (they use the macOS System
keychain via `security`). `status` works anywhere.

## Usage

```
schwab_cli cert install [--yes] [--persist-ca-key]
schwab_cli cert uninstall [--yes] [--by-label]
schwab_cli cert status
```

Normally you don't run `install` by hand: `schwab setup` runs it for you when
your Callback URL is a loopback HTTPS URL. Use these commands to re-install,
inspect, or remove the certificate.

## Subcommands

### `install`

Generates a local CA and a leaf certificate for `127.0.0.1`, then trusts the
CA in the macOS System keychain.

- You'll be asked for your **login (sudo) password** once, to add the CA to
  the System keychain (only when the CA is not already trusted — `install` is
  idempotent and re-trusts if needed).
- Prompts for confirmation first; pass `--yes` / `-y` to skip it. Running
  non-interactively without `--yes` refuses (a `sudo` prompt would be
  ambiguous).

| Flag | Purpose |
| --- | --- |
| `--yes`, `-y` | Skip the confirmation prompt. |
| `--persist-ca-key` | Keep the CA private key on disk so the leaf can be renewed automatically. **Off by default** — the CA key is transient (generated in memory, used to sign the leaf, never written) for security. Only persist it if you want automatic leaf renewal. |

### `uninstall`

Removes the Schwab CLI Local CA from the System keychain and deletes the
on-disk certificate files.

| Flag | Purpose |
| --- | --- |
| `--yes`, `-y` | Skip the confirmation prompt. |
| `--by-label` | Recovery path: remove the CA by its certificate label when no manifest is found on disk. |

### `status`

Prints the current state (works on any platform):

```
Local callback certificate status:
  CA:                trusted
  Leaf cert present: yes
  Leaf key present:  yes
  Leaf valid until:  2036-05-29T00:00:00+00:00
  Manifest present:  yes
```

- **CA** — whether the local CA is currently trusted by the OS.
- **Leaf cert / key present** — whether the leaf certificate and its key are
  on disk.
- **Leaf valid until** — the leaf's expiry (`—` if no leaf is present).
- **Manifest present** — whether `manifest.json` (the install record) exists.

## Trust model

- The CA is a **self-signed, name-constrained** root: a critical
  `NameConstraints` extension permits only `127.0.0.1/32`, so even if its key
  leaked it cannot vouch for any public hostname — only the loopback address.
- The **CA private key is transient by default** — created in memory, used
  once to sign the leaf, and never written to disk. Pass `--persist-ca-key`
  only if you want the CLI to renew the leaf automatically later.
- Only the **leaf** certificate + key are kept on disk; the callback server
  loads them at auth time.

## File layout

All artifacts live under `~/.config/schwab_cli/certs/` (honouring
`SCHWAB_CLI_CONFIG_DIR`), each written `0600` with the parent dir `0700`:

```
~/.config/schwab_cli/certs/
├── ca.pem            # local CA certificate (public)
├── ca-key.pem        # CA private key — only when --persist-ca-key is used
├── 127.0.0.1.pem     # leaf certificate served by the callback server
├── 127.0.0.1-key.pem # leaf private key
└── manifest.json     # install record (CA fingerprint, CN, created-at)
```

## Uninstalling cleanly

```bash
schwab_cli cert uninstall
```

This untrusts the CA in the System keychain (asking for your sudo password)
and deletes the files above. If the manifest is gone but a stale CA remains
trusted, recover with:

```bash
schwab_cli cert uninstall --by-label
```

## Troubleshooting

- **"run `schwab cert install` first"** during `auth` — the leaf certificate
  isn't on disk. Run `schwab cert install`, then retry. See [auth](auth.md).
- **Not on macOS** — `install` / `uninstall` exit with a message; the local
  callback flow currently requires macOS. `status` still works.
