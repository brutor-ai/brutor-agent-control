# Getting started with the Admin Console, Community Edition

Running the console from source against a Control Plane on the same machine,
signing in, and where to go first. To run the whole product from its images
instead, use the Community package: its own guide covers that.

## What you need

- Node.js `^20.19.0`, `^22.13.0` or `>=24`. `.nvmrc` pins 22.22.0
  (`nvm use` picks it up).
- A Control Plane you can reach from the browser, by default at
  `http://localhost:5050`. Starting one from source is described in the
  Control Plane's README (`python main.py`; it needs PostgreSQL and Redis, and
  on its first start the variable `BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD`).
- The Control Plane must accept the console's origin. `BRUTOR_CORS_ORIGINS`
  holds `http://localhost:3002` by default (the Control Plane's
  `.env.example`).

The console is useful with the Control Plane alone. A few screens also call
the Core Gateway directly (by default `http://localhost:8100`): health probes
and the proxy routes.

## Start the console

```bash
npm install
npm run dev
```

The development server listens on <http://localhost:3002>.

With no configuration the console calls the Control Plane at
`http://localhost:5050` and the Core Gateway at `http://localhost:8100`. To
point it elsewhere, copy the example file and edit the two addresses:

```bash
cp .env.example .env
```

| Key | Default | What it is |
|---|---|---|
| `VITE_CONTROL_PLANE_PUBLIC_URL` | `http://localhost:5050` | The Control Plane, as the browser reaches it. |
| `VITE_GATEWAY_PUBLIC_URL` | `http://localhost:8100` | The Core Gateway, as the browser reaches it. |

Both values end up in the bundle the browser downloads: put no secret in them.
Restart `npm run dev` after changing `.env`. No other key of `.env` is read.

## Sign in

Open <http://localhost:3002>. The form asks for a user name and a password;
the Community console has no tenant field.

- **User name:** the Control Plane's `BRUTOR_DEFAULT_TENANT_ADMIN_USERNAME`
  (`admin` in its `.env.example`).
- **Password:** the value of `BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD` the
  Control Plane had on its first start, when it created the administrator.

There is no stock password and the console prints none. If you run the
Community package rather than a Control Plane from source, the password was
generated for your download and is in the package's `.env`, under the same
name.

Things to know about the form:

- A wrong password and a locked account are refused in the same words
  ("Authentication failed"). See
  [Administrator sign-in lock](#administrator-sign-in-lock).
- If the administrator has two-factor authentication, the form asks for the
  six-digit code (or a backup code) after the password. If the installation
  requires two-factor authentication and the administrator has none yet, the
  form leads through setting it up before it signs in.

## Administrator sign-in lock

The Control Plane locks an administrator's account after five wrong passwords
in a row. The lock lasts 15 minutes and ends by itself. A wrong current
password on the change-password form counts as a wrong password at sign-in
does.

- **A lock stops new sign-ins only.** A session that is already open keeps
  working.
- **What the console says.** At the password step a locked account gets the
  same words as a wrong password. After that step (the two-factor code,
  two-factor set-up at sign-in) and on the change-password form, the console
  says that the account is locked and until when, in your local time, or that
  an administrator must unlock or reactivate it.
- **Unlock in the console.** Administration → Users → Admin Users & Roles. A
  locked administrator is marked **Locked**, and its row has an Unlock button.
  It asks for confirmation, then clears the lock and the count of wrong
  passwords. The button is shown to administrators whose role has the
  `admin:user:update` permission.
- **Unlock from the command line.** When no administrator is left who can
  sign in, run this in the Control Plane (`brutor-gateway-control`), against
  the same database:

  ```bash
  python main.py admin-unlock USERNAME [--tenant default]
  ```

  `--tenant` is the administrator's tenant and defaults to `default`, the
  Community Edition's one tenant. The command clears the lock and the count
  of wrong passwords and records that in the tenant's admin audit log. It
  does not change the password or reactivate a deactivated account. It exits
  0 when the account is unlocked (also when it was not locked), 2 when there
  is no such administrator, 1 when it failed.

## First steps

The console opens on **Mission Control → Overview**.

1. **Change the password.** Administration → Users → Tenant Admin Profile.
   The Profile tab has the change-password form (it asks for the current
   password; five wrong ones lock the account for 15 minutes, see above). The Security
   tab sets up two-factor authentication.
2. **Add a model.** AI Estate → Resources → AI Models: a provider under
   Providers, then a model under Configuration. Browse and Search add a model
   from the catalog. An entry marked **Needs API URL** (an Azure resource, a
   self-hosted server: a model the gateway knows no address for) asks for the
   address of your own endpoint before it is added. A model typed in by hand
   is asked for it on the form's API tab; when the catalog has no entry of
   that provider, the Control Plane refuses a save without one and the form
   shows its reason next to the API URL field.
3. **Create a resource group and an API key.** AI Estate → Resource Groups.
   A group's API Keys tab issues the keys agents and applications call the
   Core Gateway with; its Resources tab says which models, MCP servers,
   skills and agents the group may use.
4. **Watch a call arrive.** Operations → Logs & Audit → Proxy Logs lists the
   calls the Core Gateway served. Actions held for a person to decide are
   under Mission Control → Approvals.

What each screen does, and how to call the Core Gateway with the key, is in
the product documentation: <https://docs.brutor.ai>.

## Checks before you change the code

```bash
npm run type-check
npm run lint
npm run test:run
npm run build
npm run scan:bundle
```

The README says what each one holds.
