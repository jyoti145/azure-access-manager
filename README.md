# Azure Access Manager

Give people access to Azure, set budgets, and see who has what — from a simple web UI or the command line.

You describe the people and the access they should have. The tool checks Azure, shows you exactly what will change, and applies only what is missing. Running it again changes nothing.

**What it does**

- Finds users in Microsoft Entra ID, and can invite guests (for example Gmail users) when you allow it
- Creates resource groups
- Assigns any Azure role (Reader, Contributor, Owner, custom roles, …) on a management group, subscription, resource group or single resource
- Creates budgets with email alerts (for example at 50%, 75%, 90% and 100% of the budget)
- Shows spending against each budget
- Reports on the whole subscription: resource groups, resources, role assignments and cost (table, JSON or CSV)
- Audits for risky access, deleted users who still have roles, and resource groups without budgets

**Safe by design:** every change is previewed first, high-privilege roles (such as Owner) need explicit confirmation, and the tool never deletes anything or removes anyone's access.

---

## Contents

1. [What you need](#1-what-you-need)
2. [Download and install](#2-download-and-install)
3. [Sign in to Azure](#3-sign-in-to-azure)
4. [Use the web UI](#4-use-the-web-ui)
5. [Use the command line](#5-use-the-command-line)
6. [Azure permissions required](#6-azure-permissions-required)
7. [Configuration reference](#7-configuration-reference)
8. [Example config](#8-example-config)
9. [How setup works](#9-how-setup-works)
10. [Dry run](#10-dry-run)
11. [RBAC management](#11-rbac-management)
12. [Budget management](#12-budget-management)
13. [Cost tracking](#13-cost-tracking)
14. [Reports and audit](#14-reports-and-audit)
15. [Troubleshooting](#15-troubleshooting)
16. [Security](#16-security)
17. [Azure API permissions](#17-azure-api-permissions)
18. [Azure limitations](#18-azure-limitations-and-how-the-tool-handles-them)
19. [Project structure and tests](#19-project-structure-and-tests)

---

## 1. What you need

| Requirement | Why | Windows | macOS | Linux (Ubuntu/Debian) |
|---|---|---|---|---|
| **Git** | To download (clone) the project | `winget install Git.Git` | `xcode-select --install` | `sudo apt install git` |
| **Python 3.11 or newer** | Runs the tool | `winget install Python.Python.3.12` | `brew install python@3.12` | `sudo apt install python3 python3-venv python3-pip` |
| **Azure CLI** (recommended) | Signs you in to Azure | `winget install Microsoft.AzureCLI` | `brew install azure-cli` | `curl -sL https://aka.ms/InstallAzureCLIDeb \| sudo bash` |
| **An Azure account with a subscription** | The thing being managed | [Free account](https://azure.microsoft.com/free) or [Azure for Students](https://azure.microsoft.com/free/students) | | |

- On Windows you can also install Python from [python.org](https://www.python.org/downloads/). Tick **"Add python.exe to PATH"** during installation.
- The Azure CLI is optional: the web UI also has a **browser sign-in** that works without it.
- **After installing anything, close and reopen your terminal** (in VS Code, restart VS Code). Otherwise the new commands are not found.

Check that everything is installed:

```bash
git --version
python --version        # macOS / Linux: python3 --version   (must be 3.11 or newer)
az --version            # optional
```

Using the tool is free: resource groups, role assignments, budgets and guest invitations cost nothing in Azure. Costs only arise if someone you give access to creates paid resources (for example virtual machines).

---

## 2. Download and install

### Clone the project

```bash
git clone https://github.com/jyoti145/azure-access-manager.git
cd azure-access-manager
```

### Option A — one click (recommended)

**Windows:** double-click **`start-ui.bat`** (or run `.\start-ui.bat` in PowerShell).

**macOS / Linux:**

```bash
chmod +x start-ui.sh
./start-ui.sh
```

The first run creates a private Python environment (`.venv`), installs everything from `requirements.txt` (takes a minute or two), then opens the web UI in your browser. Next time it starts in seconds.

### Option B — step by step

**Windows (PowerShell):**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell says *"running scripts is disabled on this system"*, run this once and try again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

**macOS / Linux:**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

You will see `(.venv)` at the start of your prompt. Activate the environment again whenever you open a new terminal:
`.venv\Scripts\Activate.ps1` (Windows) or `source .venv/bin/activate` (macOS/Linux).

Check the installation (no Azure needed):

```bash
python azure_manager.py --help
python -m pytest -q
```

---

## 3. Sign in to Azure

```bash
az login
```

A browser window opens. Sign in with your Azure account (work, school or personal, e.g. Gmail). If you have several subscriptions, the terminal asks you to pick one — type its number and press Enter.

Check who you are signed in as:

```bash
az account show -o table
```

**If something goes wrong:**

| Problem | Fix |
|---|---|
| A Windows account popup appears and gets stuck | `az config set core.enable_broker_on_windows=false`, then `az login` again (a browser opens instead) |
| No browser opens | `az login --use-device-code`, then open https://microsoft.com/devicelogin and enter the code shown |
| *"must use multi-factor authentication"* / *"No subscriptions found"* | `az login --tenant <tenant-id>` — the tenant ID is shown in the error message. Complete the MFA step (Microsoft Authenticator). |
| Still *"No subscriptions found"* | Your account has no subscription yet. Create one at https://portal.azure.com (search "Subscriptions"). |

Your password and tokens are never stored by this tool. It uses your Azure CLI sign-in (or the browser sign-in in the UI).

---

## 4. Use the web UI

```bash
python azure_manager.py ui
```

(or `start-ui.bat` / `./start-ui.sh`). The UI opens at **http://localhost:8501**. Keep the terminal open while you use it; press `Ctrl + C` there to stop it.

**First time:**

1. **Account** — if you are signed in with `az login`, your subscriptions load automatically. Otherwise click **Load my subscriptions**. Pick your subscription and click **Use this subscription**.
2. **Users & access → Add user** — enter the person's email, choose a role (start with **Reader**, which can only view), a resource group and an optional budget. Click **Add user**.
3. **Apply changes** — click **Preview changes**. Nothing is changed yet. Check the plan, tick **"I have reviewed the changes"**, then click **Apply**.
4. Run **Preview changes** again: it should say everything is already set up.

| Page | What it does |
|---|---|
| **Dashboard** | Subscription details, cost this month, resource groups with budget usage, JSON/CSV download |
| **Users & access** | Add or remove people with a form, or edit `config.yaml` directly |
| **Apply changes** | Preview → permission check → apply. Owner-level roles need an extra confirmation. |
| **Budgets** | Budget, spent, remaining, forecast and thresholds reached |
| **Access** | Every role assignment, marked *Direct* or *Inherited*, with filters |
| **Resources** | Resource groups and resources with their status |
| **Audit** | Risky or unusual access, deleted users with roles, resource groups without budgets |
| **Account** | Sign in, switch account or subscription, sign in to an MFA tenant |

**Your settings are personal.** Your `config.yaml` (subscription, emails) is created from `config.example.yaml` on first run and is never uploaded to Git (it is in `.gitignore`).

**The UI only runs on your own computer** (`localhost`). It acts with *your* Azure sign-in, so nobody else on your network can open it. Everyone who wants to use the tool clones it and runs it on their own machine with their own Azure account.

---

## 5. Use the command line

Everything in the UI is also available as commands — useful for scripts and automation.

```bash
# 1. Create your configuration from the template
copy config.example.yaml config.yaml      # Windows
cp config.example.yaml config.yaml        # macOS / Linux
```

2. Open `config.yaml`, set `subscription_id` (from `az account show -o table`) and add your users (see [Example config](#8-example-config)).

```bash
# 3. Check the file (no Azure calls)
python azure_manager.py validate

# 4. Preview what would change (changes nothing)
python azure_manager.py setup --dry-run

# 5. Apply (asks for confirmation)
python azure_manager.py setup
```

| Command | What it does |
|---|---|
| `python azure_manager.py ui` | Open the web UI |
| `python azure_manager.py validate` | Check `config.yaml` offline |
| `python azure_manager.py setup --dry-run` | Preview changes |
| `python azure_manager.py setup` | Apply `config.yaml` (add `--yes` for unattended runs) |
| `python azure_manager.py budget-status` | Spend against every configured budget |
| `python azure_manager.py budgets` | All budgets in the subscription |
| `python azure_manager.py rbac` | All role assignments (`--resource-group <name>` for one group) |
| `python azure_manager.py users` | Are the configured users in the directory, and what roles do they have |
| `python azure_manager.py resource-groups` | Resource groups with resource count and cost |
| `python azure_manager.py resources` | Resources grouped by resource group |
| `python azure_manager.py subscription-report` | Full report (`--format json\|csv`, `--export` to save files) |
| `python azure_manager.py audit` | Security and cost review (read-only) |

Add `--help` to any command for all options, e.g. `python azure_manager.py setup --help`.

**Other ways to sign in** (all commands accept `--auth`):

```bash
python azure_manager.py setup --dry-run --auth browser   # browser sign-in, no Azure CLI needed
```

For automation (CI/CD), use a service principal through environment variables — the tool picks them up automatically:

```bash
export AZURE_TENANT_ID="<tenant-guid>"
export AZURE_CLIENT_ID="<app-id>"
export AZURE_CLIENT_SECRET="<secret>"              # or AZURE_CLIENT_CERTIFICATE_PATH=/path/cert.pem
export AZURE_SUBSCRIPTION_ID="<subscription-guid>" # optional
```

(PowerShell: `$env:AZURE_CLIENT_SECRET = "..."`.) Never put credentials in `config.yaml`; the tool refuses to load a config that contains them.

---

## 6. Azure permissions required

Grant the **account that runs the tool** only what you use (least privilege):

| Task | Minimum role | Scope |
|---|---|---|
| Read subscription, resource groups, resources | Reader | Subscription |
| Create resource groups | Contributor (or a custom role with `Microsoft.Resources/subscriptions/resourceGroups/write`) | Subscription |
| Create role assignments | **Role Based Access Control Administrator** (preferred; can be limited to specific roles) or User Access Administrator | Each target scope or a parent |
| Create/update budgets | Cost Management Contributor | Each budget scope (resource group or subscription) |
| Read cost data / budget status | Cost Management Reader | Subscription |
| Look up users | Entra ID: `User.Read.All` (members can usually read users by default) | Tenant |
| Invite guests (only if `invite_if_missing: true`) | Entra ID: `User.Invite.All` or the **Guest Inviter** directory role | Tenant |

If you created the subscription yourself (for example a free or student subscription), you are its **Owner** and already have everything above.

## 7. Configuration reference

`config.yaml` (or `config.json`) holds the target subscription and a list of users. Check it offline with:

```bash
python azure_manager.py validate
```

Every problem in the file is reported at once.

### Top level

| Key | Required | Description |
|---|---|---|
| `subscription_id` | yes* | Target subscription GUID. *Or pass `--subscription` / set `AZURE_SUBSCRIPTION_ID`. |
| `tenant_id` | no | Expected tenant. The run stops on a mismatch. |
| `defaults` | no | `location`, `invite_if_missing`, `send_invitation_email`, `invite_redirect_url`, `budget` (merged into every user's budget) |
| `users` | yes | List of users (below) |
| `subscription_report` | no | `enabled`, `include_resources`, `include_costs`, `include_rbac`. When enabled, `setup` writes a report afterwards. |

### User

| Key | Description |
|---|---|
| `name`, `email` | Display name and email (email or `object_id` required) |
| `object_id` | Optional Entra object ID. Skips the email lookup and is useful when an email matches several accounts. |
| `invite_if_missing` | `false` by default. If `true`, a missing user gets a B2B guest invitation. |
| `send_invitation_email` | Default `true` |
| `resource_group` | `name`, `location`, `create_if_missing` (default `false`), `tags` |
| `access` | One mapping or a list of mappings (below) |
| `budget` | Budget mapping (section 12) |

### Access entry

| Key | Description |
|---|---|
| `role` | Any role name, case-insensitive (`Reader`, `Contributor`, `Owner`, `User Access Administrator`, `Storage Blob Data Reader`, custom roles, ...) or a role definition GUID |
| `scope_type` | `management_group`, `subscription`, `resource_group`, `resource` (aliases: `mg`, `sub`, `rg`) |
| `resource_group` | For `resource_group` scope. Defaults to the user's `resource_group.name`. |
| `management_group_id` | For `management_group` scope |
| `resource_id` | Full resource ID for `resource` scope |
| `scope` | Either a full ARM scope ID (`/subscriptions/.../resourceGroups/rg-x`) or a scope-type keyword (`scope: resource_group`) |

`scope_type` is never assumed to be `subscription`. If it cannot be inferred from the entry, validation fails rather than silently granting broader access.

## 8. Example config

```yaml
subscription_id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

defaults:
  location: centralindia
  invite_if_missing: false

users:
  - name: "John Doe"
    email: "john@example.com"
    resource_group:
      name: "rg-john"
      location: "centralindia"
      create_if_missing: true
    access:
      role: "Contributor"
      scope: "resource_group"
    budget:
      enabled: true
      amount: 10000
      currency: "INR"
      period: "monthly"
      thresholds: [50, 75, 90, 100]
      notifications:
        emails: ["admin@example.com"]

  - name: "Alice"
    email: "alice@example.com"
    access:
      scope_type: "subscription"
      role: "Reader"

  - name: "Bob"
    email: "bob@example.com"
    invite_if_missing: true
    access:
      - scope_type: resource_group
        resource_group: rg-development
        role: Contributor
      - scope: /subscriptions/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx/resourceGroups/rg-data/providers/Microsoft.Storage/storageAccounts/stdata
        role: Storage Blob Data Reader
    budget:
      enabled: true
      resource_group: rg-development
      amount: 500
      currency: INR
      thresholds:
        - 80
        - { threshold: 100, type: forecasted, emails: [finance@example.com] }

subscription_report:
  enabled: true
```

## 9. How setup works

```bash
python azure_manager.py setup --dry-run     # always preview first
python azure_manager.py setup               # shows the plan, asks for confirmation, applies
python azure_manager.py setup --yes         # unattended (CI); validation and pre-flight still run
```

`setup` runs in this order:

1. **Authenticate** and show the identity and tenant.
2. **Validate** the subscription (exists, enabled, same tenant).
3. **Plan** each user using read-only calls:
   - Look up the user, or plan an invitation.
   - Check the resource group exists, or plan to create it.
   - Resolve the role and check whether the assignment already exists.
   - Compare the budget with the configuration.
4. **Pre-flight check** of the permissions the plan needs. The checks are exact: for example, `roleAssignments/write` is checked at each target scope, and at the subscription when the resource group does not exist yet. If a check fails, the tool stops before making any change.
5. **Confirm.** Owner, User Access Administrator, RBAC Administrator and any management-group assignment each need explicit confirmation, then the whole change set is confirmed. `--yes` skips the prompts but never skips validation.
6. **Apply** in order: invite → resource group → role assignment → budget. If an action fails, the remaining actions for that user stop. Other users continue.
7. **Summarise**: per-user result, current spend, budget usage and an overall `STATUS`.
8. **Report**: writes `reports/subscription-report-YYYY-MM-DD.{json,csv}` when `subscription_report.enabled`. Skip it with `--no-report`.

Useful options:

```bash
python azure_manager.py setup --user john@example.com        # one user only
python azure_manager.py setup --only rbac,budgets            # subset of stages: users,resource-groups,rbac,budgets
python azure_manager.py setup -c other-config.yaml -s <sub>  # different file / subscription
```

**Idempotency.** A second run finds everything in place:

```
  ✓ User exists
  ✓ Resource Group exists: rg-john
  ✓ Contributor role already assigned
  ✓ Budget already exists: budget-rg-john
  ✓ Budget notifications already configured

Setup is already complete.
STATUS: NO CHANGES REQUIRED
```

**Exit codes:** `0` success / no changes, `1` error, `2` invalid configuration, `3` pre-flight failed, `4` partial (some actions failed or users were blocked), `5` aborted at a confirmation prompt.

## 10. Dry run

`--dry-run` builds the same plan a real run would. Planning only performs reads, so a dry run never changes anything:

```
DRY RUN

Subscription:
  Production Subscription

User:
  John Doe
  john@example.com

Actions:
  ✓ User exists
  [CREATE]   Resource Group: rg-john
             Location: centralindia
  [ASSIGN]   Contributor
             Scope: Resource Group: rg-john
  [CREATE]   Budget: budget-rg-john
             Amount: ₹10,000
             Period: Monthly
             Alerts (actual): 50%, 75%, 90%, 100%
             Recipients: admin@example.com

No changes were made.
```

The dry run also runs the pre-flight check and exits with code `3` if a real run would be refused, which is useful as a CI gate.

## 11. RBAC management

- Roles are resolved **dynamically**. No role definition IDs are hard-coded. A typo gets suggestions, for example: `Role 'Contributer' does not exist ... Did you mean: Contributor?`
- Assignments always use the principal's **object ID**. `principalType` is set so that newly invited users don't fail because of directory replication delay. `PrincipalNotFound` is retried with backoff.
- **Already assigned** means the same principal has the same role at exactly the same scope. In that case nothing is created.
- An assignment of the same role at a **parent** scope is shown as inherited, and the direct assignment is still created, so the config stays the source of truth.
- Inheritance only flows **downwards**. Owner on a resource group is never treated as Owner on the subscription.
- The tool **never removes** access. The `audit` command reports unmanaged and orphaned assignments, along with the `az` command to remove one after review.

```bash
python azure_manager.py rbac                                  # all assignments effective in the subscription
python azure_manager.py rbac --resource-group rg-john         # effective on one RG (Direct vs Inherited)
python azure_manager.py rbac --user john@example.com --format csv
python azure_manager.py users                                 # configured users: found? guest? current roles
```

## 12. Budget management

```yaml
budget:
  enabled: true
  name: budget-rg-john            # default: budget-<resource group>
  amount: 10000
  currency: INR                   # see note below
  period: monthly                 # monthly | quarterly | annually
  scope: resource_group           # or subscription
  resource_group: rg-john         # default: the user's resource group
  start_date: 2026-10-01          # optional, must be the 1st of a month (default: current month)
  end_date: 2027-09-30            # optional
  thresholds: [50, 75, 90, 100]   # actual-spend alerts, % of amount
  forecast_thresholds: [100]      # forecasted-spend alerts
  notifications:
    enabled: true
    emails: [admin@example.com, finance@example.com]
    roles: [Owner]                # notify holders of these roles on the scope
    action_groups: []             # action group resource IDs
```

Individual thresholds can override settings:
`- { threshold: 110, type: forecasted, operator: GreaterThan, emails: [cfo@example.com] }`.

The alert model (`AlertSpec`) is separate from the budget, so new alert types or recipient kinds can be added in one place (`src/budgets.py: build_notification`).

- If no recipients are configured, alerts go to the user's own email. The plan says so explicitly.
- Changing the amount, period, thresholds or recipients in the config produces an `[UPDATE]` on the next run. The update uses the budget's eTag and keeps its original start date.
- **Currency:** Azure budgets have no currency field. Amounts are always in the subscription's **billing currency**. `currency` in the config is used for display, and you get a warning if it differs from the billing currency. The amount is **not converted**.

```bash
python azure_manager.py budgets                       # all budgets: amount, spend, utilisation, alerts
python azure_manager.py budgets --resource-group rg-john
```

## 13. Cost tracking

```bash
python azure_manager.py budget-status --resource-group rg-john
python azure_manager.py budget-status                  # every budget defined in config.yaml
python azure_manager.py budget-status --format json
```

```
Resource Group: rg-john

Budget:           ₹10,000  (budget-rg-john, Monthly)
Current Spend:    ₹7,600
Remaining:        ₹2,400
Usage:            76.0%
Forecast:         ₹10,400 (104.0%)

Status:           WARNING

Thresholds:
  50%             ●  exceeded
  75%             ●  exceeded
  90%             ○
  100%            ○
WARNING: 50% budget threshold exceeded (budget-rg-john).
WARNING: 75% budget threshold exceeded (budget-rg-john).
```

Status bands: `HEALTHY` < 75% ≤ `WARNING` < 90% ≤ `CRITICAL` < 100% ≤ `EXCEEDED`.

Budget spend comes from the budget itself (`currentSpend`/`forecastSpend` for the budget period). Month-to-date and previous-month cost come from the Cost Management Query API, using one grouped query per period to stay within throttling limits.

## 14. Reports and audit

```bash
python azure_manager.py subscription-report                    # table
python azure_manager.py subscription-report --format json      # machine-readable on stdout
python azure_manager.py subscription-report --format csv
python azure_manager.py subscription-report --export           # also writes reports/subscription-report-YYYY-MM-DD.{json,csv}
python azure_manager.py subscription-report --no-resources --no-costs   # faster, partial
```

Sections:

- **Subscription:** name, ID, tenant, state, placement "location", offer, spending limit, tags.
- **Cost:** current month, previous month, monthly budget and utilisation, cost outside any resource group.
- **Resource groups:** location, resource count, budget, MTD cost, budget spend, utilisation, status.
- **Users / RBAC:** principal, email, type (guest flagged), role, scope, scope type, Direct/Inherited, assignment ID.
- **Resources** grouped by resource group: name, type, region, status. Status is the VM power state where available.

The CSV is a single file with a `record_type` column (`subscription`, `cost_summary`, `resource_group`, `budget`, `role_assignment`, `resource`, `warning`), so it loads straight into Excel or pandas. If one section is not permitted (for example, cost data on an unsupported offer), the rest of the report is still produced and a warning explains what is missing.

Other read-only commands:

```bash
python azure_manager.py resource-groups
python azure_manager.py resources --resource-group rg-john
python azure_manager.py audit                 # drift + privileged access + orphaned assignments + RGs without budgets
python azure_manager.py audit --format json --export
```

## 15. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `python` / `az` / `git` is *not recognized* | Close and reopen the terminal (restart VS Code) after installing. On Windows, reinstall Python with **"Add python.exe to PATH"** ticked. On macOS/Linux use `python3`. |
| `Python 3.11 or newer is required` | Install a newer Python (see [What you need](#1-what-you-need)) and recreate `.venv`: delete the `.venv` folder and run the install again. |
| `ModuleNotFoundError` (e.g. `No module named 'azure'`) | The virtual environment is not active or not installed: activate `.venv` and run `pip install -r requirements.txt`. |
| *Running scripts is disabled on this system* (PowerShell) | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| Port 8501 already in use | `python azure_manager.py ui --port 8502` |
| The UI says **Not connected** | Open **Account**, click **Load my subscriptions**, choose one, click **Use this subscription**. |
| `No usable Azure credential: Azure CLI not found on path` | Install the Azure CLI and run `az login`, use `--auth browser`, or set the service principal variables. |
| `Tenant mismatch` / `InvalidAuthenticationTokenTenant` | You are signed in to another tenant: `az login --tenant <tenant-id>`, or set `tenant_id` in the config. |
| `Subscription ... was not found` | Wrong ID or no access: `az account list -o table`. |
| Pre-flight: `missing Microsoft.Authorization/roleAssignments/write` | The signed-in identity cannot grant roles at that scope. Ask for *Role Based Access Control Administrator* there. |
| Pre-flight warning: `granted only with a condition` | You have a constrained RBAC Administrator assignment. Roles outside the condition will be refused at apply time. |
| `User not found in tenant ... invitation is required` | Set `invite_if_missing: true` for the user, or invite them manually in Entra ID. |
| `Insufficient privileges in Microsoft Entra ID` | Reading users needs `User.Read.All`. Inviting needs `User.Invite.All` or the Guest Inviter role. |
| `PrincipalNotFound` after an invitation | Directory replication delay. The tool retries for about 2 minutes. If it persists, run `setup` again. |
| `Role '...' does not exist` | Check the spelling (suggestions are shown) or whether a custom role is assignable at that scope. |
| `RequestDisallowedByPolicy` | Azure Policy blocked the change (for example, an allowed-locations policy on resource groups). |
| Cost data `N/A` | Cost Management Reader is missing, the offer doesn't support Cost Management, or the subscription is new. Data lags by 8-24 h. |
| `Azure throttled the request (HTTP 429)` | Cost Management is heavily rate-limited. Requests are retried automatically. Wait and re-run. |
| Need more detail | Use `--verbose`, and see `logs/azure-manager-YYYY-MM-DD.log` (JSON lines). |

## 16. Security

- **No credentials in config, logs or source.** The config loader rejects keys such as `password`, `client_secret` or `token`. Every log line passes through a redaction filter that removes bearer tokens, JWTs, secret-like `key=value` pairs and the literal value of `AZURE_CLIENT_SECRET`. Tokens are never printed. Only non-secret claims (tenant, UPN, object ID) are read to show who is signed in.
- **Least privilege.** Use the narrow roles in section 6 for the identity running the tool. Prefer *Role Based Access Control Administrator* with conditions over User Access Administrator.
- **No implicit privilege.** Owner is granted only when written in the config. Owner, User Access Administrator, RBAC Administrator and management-group assignments need explicit confirmation (`--yes` must be passed deliberately in automation).
- **No silent invitations.** External users are invited only with `invite_if_missing: true`.
- **No deletions.** The tool never deletes resource groups, budgets or role assignments, and never revokes access. Any future `remove-access` command must require explicit confirmation.
- **Input validation.** GUIDs, emails, resource group names, regions, management group IDs, resource IDs, budget names and thresholds are validated before any API call. OData and KQL values are escaped.
- **Audit trail.** Every change, confirmation and failure is logged in `logs/` as structured JSON (`timestamp, level, operation, resource, user, scope, result, error`). Role assignments carry the description "Managed by azure-access-manager".
- Keep `logs/` and `reports/` out of source control. They contain user emails and object IDs. A `.gitignore` is included.

## 17. Azure API permissions

| API | Operations used | Permission (action) |
|---|---|---|
| ARM - Subscriptions | `GET /subscriptions/{id}` | `Microsoft.Resources/subscriptions/read` |
| ARM - Resource groups | get, list, create_or_update | `Microsoft.Resources/subscriptions/resourceGroups/read`, `/write` |
| ARM - Resources / Resource Graph | list resources, power state | `Microsoft.Resources/subscriptions/resources/read` (Resource Graph uses your read access) |
| ARM - Management groups | `GET managementGroups/{id}` | `Microsoft.Management/managementGroups/read` |
| Authorization | role definitions list | `Microsoft.Authorization/roleDefinitions/read` |
| Authorization | role assignments list / create | `Microsoft.Authorization/roleAssignments/read`, `/write` |
| Authorization | effective permissions (pre-flight) | `Microsoft.Authorization/permissions/read` (always allowed for the caller) |
| Consumption | budgets get / list / create_or_update | `Microsoft.Consumption/budgets/read`, `/write` |
| Cost Management | Query API | `Microsoft.CostManagement/query/read` |
| Microsoft Graph | `GET /users`, `POST /directoryObjects/getByIds` | `User.Read.All` (or `Directory.Read.All`) |
| Microsoft Graph | `POST /invitations` | `User.Invite.All` |

For a **service principal**, grant the Graph permissions as *application* permissions with admin consent. Assign the Azure roles to the service principal at the scopes it manages.

SDKs used: `azure-identity`, `azure-mgmt-resource`, `azure-mgmt-authorization`, `azure-mgmt-consumption`, `azure-mgmt-costmanagement`. Plain HTTPS is used where an SDK does not fit:

- Subscription metadata: `azure-mgmt-resource` ≥ 24 no longer ships `SubscriptionClient`.
- Effective permissions: the SDK has no subscription-level call.
- Resource Graph.
- Microsoft Graph: the official SDK is async-only and very large.

## 18. Azure limitations and how the tool handles them

| Limitation | Handling |
|---|---|
| Budgets have no currency field. Amounts are in the billing currency. | `currency` is display-only, and a mismatch is reported as a warning. No conversion is done. |
| Max 5 notifications per budget | Validated in the config |
| Cost data lags usage by 8-24 h. Budgets are evaluated periodically. | Stated in the output. Spend is never presented as real-time. |
| Some offers (certain sponsorship/free/CSP) don't expose Cost Management | Cost sections show `N/A` with the reason. Everything else still runs. |
| Subscriptions have no region | The placement ID is shown as "location", and labelled as such |
| No universal resource "status" in Azure | VM power state (Resource Graph), otherwise the resource's `state`, otherwise the provisioning state |
| The effective-permissions API ignores deny assignments and Azure Policy | The pre-flight check can pass while an action is still denied. Such errors are reported clearly at apply time. |
| Graph invite permission can't be tested without inviting | Verified when the invitation is sent. The pre-flight check notes this. |
| Directory replication delay for new guests | `principalType` is set on assignments, and `PrincipalNotFound` is retried |
| Management group ancestry isn't in the scope path | Management-group and root assignments returned for a subscription are labelled *Inherited* |
| Public Azure cloud only | Sovereign clouds (China, US Gov) would need different endpoints and are not configured |

## 19. Project structure and tests

```
azure-access-manager/
├── azure_manager.py          # entry point (CLI; 'ui' command starts the web UI)
├── start-ui.bat / start-ui.sh  # one-click setup + start of the web UI
├── config.example.yaml       # shareable template (config.yaml is personal and git-ignored)
├── requirements.txt
├── README.md
├── .streamlit/config.toml    # binds the UI to localhost only
├── ui/                       # Streamlit web UI
│   ├── app.py                # navigation + sidebar
│   ├── common.py             # session state, sign-in, caching, error display
│   └── page_*.py             # account, dashboard, users, setup, budgets, access, resources, audit
├── src/
│   ├── accounts.py           # discover tenants + subscriptions for the signed-in identity
│   ├── cli.py                # argparse commands, exit codes, top-level error handling
│   ├── auth.py               # credential selection (CLI / service principal / browser), identity display
│   ├── config.py             # YAML/JSON loading + validation into typed models
│   ├── models.py             # dataclasses: config specs, scopes, plan actions
│   ├── validation.py         # ID / name / email validators, OData/KQL escaping
│   ├── scopes.py             # scope parsing, hierarchy, Direct vs Inherited
│   ├── context.py            # lazily-built SDK clients and services
│   ├── rest.py               # small ARM/Graph REST client with retry + SDK model normalisation
│   ├── subscriptions.py      # subscription + management group lookup
│   ├── users.py              # Entra ID lookup, guest invitations, principal resolution
│   ├── rbac.py               # role resolution, idempotent assignments, listing
│   ├── resource_groups.py    # resource group get/list/create
│   ├── budgets.py            # budget payloads, drift detection, threshold status
│   ├── costs.py              # Cost Management Query API
│   ├── resources.py          # Resource Graph inventory (ARM fallback)
│   ├── permissions.py        # effective permissions + pre-flight check
│   ├── setup_engine.py       # plan (read-only) / execute engine used by setup and audit
│   ├── reports.py            # subscription report, audit, JSON/CSV export
│   ├── views.py              # console rendering
│   ├── console.py            # symbols, colours, tables, currency formatting
│   ├── errors.py             # error types + Azure error translation
│   └── logging_utils.py      # JSON-lines logging with secret redaction
├── logs/                     # azure-manager-YYYY-MM-DD.log
├── reports/                  # exported reports
└── tests/                    # pytest suite with in-memory Azure fakes (no network)
    ├── conftest.py
    ├── test_config.py
    ├── test_validation.py
    ├── test_rbac.py
    ├── test_budget.py
    └── test_ui.py            # renders every UI page headlessly against the fakes
```

Run the tests:

```bash
python -m pytest -q
```
